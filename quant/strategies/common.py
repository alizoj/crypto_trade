"""Общие части стратегий: подготовка баров, флаги качества, HTF без look-ahead, внешние ряды.

Правила (раздел 3.2, 6 ТЗ):
- расчёт только по закрытым барам; если передан bars_1m, незакрытый последний бар ТФ отбрасывается;
- halted-бары не попадают в индикаторы и не дают сигналов (строка в кадре остаётся);
- бары с synthetic/has_synthetic не дают сигналов;
- TradFi вне сессии базового рынка (в момент close_ts) — без сигналов, если
  filters.tradfi_off_session_signals = false;
- HTF: на бар ТФ с закрытием close_ts приходится последний бар старшего ТФ с close <= close_ts
  (аналог request.security(..., lookahead_off) со сдвигом [1]);
- funding / OI / тренд BTC — последнее значение с временем <= close_ts.
"""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
from typing import Any

import numpy as np
import polars as pl

from quant.data.aggregate import resample
from quant.indicators import core
from quant.indicators import volume as vol
from quant.indicators.structure import market_structure
from quant.schemas import TS
from quant.sessions import open_mask
from quant.timeutil import tf_minutes

MS_PER_MIN = 60_000


def deep_merge(base: dict, over: dict | None) -> dict:
    """Рекурсивное слияние словарей (over поверх base), base не меняется."""
    out = deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = deepcopy(v)
    return out


def ms(s: pl.Series) -> np.ndarray:
    return s.dt.epoch("ms").to_numpy().astype(np.int64)


def _norm_ts(df: pl.DataFrame, col: str = "ts") -> pl.DataFrame:
    dt = df.schema[col]
    expr = pl.col(col)
    if isinstance(dt, pl.Datetime) and dt.time_zone is None:
        expr = expr.dt.replace_time_zone("UTC")
    return df.with_columns(expr.cast(TS).alias(col))


def prepare_bars(bars_tf: pl.DataFrame, tf: str, bars_1m: pl.DataFrame | None = None) -> pl.DataFrame:
    """Сортировка, ts → Datetime(ms, UTC), close_ts = ts + tf; незакрытый бар (по bars_1m) — прочь."""
    if bars_tf is None or bars_tf.is_empty():
        return pl.DataFrame()
    df = _norm_ts(bars_tf).sort("ts").unique(subset=["ts"], keep="last", maintain_order=True)
    df = df.with_columns((pl.col("ts") + pl.duration(minutes=tf_minutes(tf))).alias("close_ts"))
    if bars_1m is not None and not bars_1m.is_empty():
        last_ms = int(ms(_norm_ts(bars_1m.select("ts"))["ts"]).max()) + MS_PER_MIN
        df = df.filter(pl.col("close_ts").dt.epoch("ms") <= last_ms)
    return df


def bar_flags(df: pl.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    """(halted, blocked): halted — бар не участвует ни в чём; blocked — нет сигналов (synthetic)."""
    n = df.height
    halted = df["halted"].fill_null(False).to_numpy().astype(bool) if "halted" in df.columns else np.zeros(n, bool)
    blocked = np.zeros(n, dtype=bool)
    for c in ("synthetic", "has_synthetic"):
        if c in df.columns:
            blocked |= df[c].fill_null(False).to_numpy().astype(bool)
    return halted, blocked


def session_ok(close_ms: np.ndarray, instrument: Any, filters: dict) -> np.ndarray:
    """TradFi: сигналы только при открытой сессии базового рынка в момент close_ts (правило 3.1.6)."""
    if instrument is None or not getattr(instrument, "is_tradfi", False):
        return np.ones(len(close_ms), dtype=bool)
    if filters.get("tradfi_off_session_signals", False):
        return np.ones(len(close_ms), dtype=bool)
    return open_mask(instrument.underlying_type, close_ms)


def asof_idx(ref_ms: np.ndarray, query_ms: np.ndarray) -> np.ndarray:
    """Индекс последнего ref <= query (ref отсортирован), -1 — нет такого."""
    return np.searchsorted(ref_ms, query_ms, side="right") - 1


def asof_values(ref_ms: np.ndarray, values: np.ndarray, query_ms: np.ndarray) -> np.ndarray:
    idx = asof_idx(ref_ms, query_ms)
    out = np.full(len(query_ms), np.nan)
    ok = idx >= 0
    out[ok] = values[idx[ok]]
    return out


@lru_cache(maxsize=4)
def prop_boundary_min(market: str = "crypto_tradfi") -> int:
    """Граница проп-дня из challenge.yaml (для vwap_anchor=prop_day)."""
    try:
        from quant.config import load_challenge_config

        return load_challenge_config().market(market).day_boundary_min
    except Exception:  # noqa: BLE001 — без конфига: дефолт раздела 2.2
        return vol.PROP_DAY_BOUNDARY_MIN


# ---------------------------------------------------------------- HTF


def htf_frame(htf: pl.DataFrame | None, bars_1m: pl.DataFrame | None, bars_tf: pl.DataFrame,
              htf_tf: str) -> pl.DataFrame | None:
    """Бары старшего ТФ: переданные, иначе агрегация bars_1m, иначе агрегация баров ТФ.
    Добавляет close_ms; halted-бары удаляются."""
    if htf is not None and not htf.is_empty():
        h = _norm_ts(htf).sort("ts")
    elif bars_1m is not None and not bars_1m.is_empty():
        h = resample(_norm_ts(bars_1m).sort("ts"), htf_tf)
    elif bars_tf is not None and not bars_tf.is_empty():
        src = bars_tf.drop([c for c in ("close_ts", "n_min") if c in bars_tf.columns])
        h = resample(src, htf_tf)
    else:
        return None
    if h.is_empty():
        return None
    if "halted" in h.columns:
        h = h.filter(~pl.col("halted").fill_null(False))
    return h.with_columns((pl.col("ts").dt.epoch("ms") + tf_minutes(htf_tf) * MS_PER_MIN).alias("close_ms"))


def htf_block(hdf: pl.DataFrame | None, close_ms: np.ndarray, ema_len: int, left: int,
              right: int) -> dict[str, np.ndarray]:
    """HTF-тренд: EMA(ema_len) и структура HH/HL на закрытых барах HTF, выровненные по close_ts.

    htf_trend = +1, если close HTF > EMA и структура не LH/LL; -1 — зеркально; иначе 0.
    """
    n = len(close_ms)
    nan = np.full(n, np.nan)
    if hdf is None or hdf.is_empty():
        return {"htf_close": nan, "htf_ema": nan.copy(), "htf_ema_side": np.zeros(n, np.int8),
                "htf_struct": np.zeros(n, np.int8), "htf_trend": np.zeros(n, np.int8),
                "htf_ready": np.zeros(n, bool)}
    hc = hdf["close"].to_numpy().astype(np.float64)
    hh = hdf["high"].to_numpy().astype(np.float64)
    hl = hdf["low"].to_numpy().astype(np.float64)
    e = core.ema(hc, ema_len)
    st = market_structure(hh, hl, hc, left, right)
    idx = asof_idx(hdf["close_ms"].to_numpy().astype(np.int64), close_ms)
    ok = idx >= 0
    j = np.where(ok, idx, 0)
    a_close = np.where(ok, hc[j], np.nan)
    a_ema = np.where(ok, e[j], np.nan)
    a_struct = np.where(ok, st.hh_hl[j], 0).astype(np.int8)
    ready = ~np.isnan(a_ema) & ~np.isnan(a_close)
    side = np.where(ready, np.sign(a_close - np.where(ready, a_ema, 0.0)), 0).astype(np.int8)
    trend = np.where((side > 0) & (a_struct >= 0), 1, np.where((side < 0) & (a_struct <= 0), -1, 0)).astype(np.int8)
    return {"htf_close": a_close, "htf_ema": a_ema, "htf_ema_side": side, "htf_struct": a_struct,
            "htf_trend": trend, "htf_ready": ready}


# ---------------------------------------------------------------- индикаторы ТФ


def indicator_block(h: np.ndarray, lo: np.ndarray, c: np.ndarray, v: np.ndarray, tb: np.ndarray | None,
                    ts_ms: np.ndarray, p: dict, boundary_min: int) -> dict[str, np.ndarray]:
    """Индикаторы ТФ, общие для стратегий и скринера: rsi14, ATR, DMI/ADX, VWAP, RVOL, CVD."""
    out: dict[str, np.ndarray] = {}
    out["rsi14"] = core.rsi(c, 14)
    a = core.atr(h, lo, c, int(p.get("atr_len", 14)))
    out["atr"] = a
    with np.errstate(divide="ignore", invalid="ignore"):
        out["atr_pct"] = a / c * 100.0
    adx_len = int(p.get("adx_len", 14))
    out["plus_di"], out["minus_di"], out["adx"] = core.dmi(h, lo, c, adx_len, adx_len)
    key = vol.session_key(ts_ms, p.get("vwap_anchor", "utc_day"), boundary_min)
    vw = vol.vwap(core.hlc3(h, lo, c), v, key)
    out["vwap"] = vw
    with np.errstate(divide="ignore", invalid="ignore"):
        out["vwap_dist_pct"] = (c - vw) / vw * 100.0
    out["rvol"] = vol.rvol(v, int(p.get("rvol_len", 20)))
    out.update(cvd_block(c, v, tb, int(p.get("cvd_len", 24)), float(p.get("cvd_thr", 0.05))))
    return out


def cvd_block(c: np.ndarray, v: np.ndarray, tb: np.ndarray | None, n: int, thr: float) -> dict[str, np.ndarray]:
    """CVD-тренд и CVD-дивергенция по окну n баров (не зависят от уровня кумулятивы).

    ratio = Σ delta / Σ volume за n баров; cvd_trend = sign при |ratio| > thr.
    Медвежья CVD-дивергенция: close — новый максимум за n баров, а Σ delta < 0; бычья — зеркально.
    """
    m = len(c)
    if tb is None or np.all(np.isnan(tb)):
        return {"cvd": np.full(m, np.nan), "cvd_ratio": np.full(m, np.nan), "cvd_trend": np.zeros(m, np.int8),
                "cvd_div_bull": np.zeros(m, bool), "cvd_div_bear": np.zeros(m, bool)}
    delta = vol.cvd_delta(v, tb)
    sd = core.rolling_sum(delta, n)
    sv = core.rolling_sum(v, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(sv > 0, sd / sv, np.nan)
    trend = np.where(ratio > thr, 1, np.where(ratio < -thr, -1, 0)).astype(np.int8)
    prev_hi = core.shift(core.highest(c, n), 1)
    prev_lo = core.shift(core.lowest(c, n), 1)
    with np.errstate(invalid="ignore"):
        div_bear = (c > prev_hi) & (sd < 0)
        div_bull = (c < prev_lo) & (sd > 0)
    return {"cvd": vol.cvd(v, tb), "cvd_ratio": ratio, "cvd_trend": trend,
            "cvd_div_bull": div_bull, "cvd_div_bear": div_bear}


# ---------------------------------------------------------------- внешние ряды (meta)


def _pick(df: pl.DataFrame, names: tuple[str, ...]) -> str | None:
    for n in names:
        if n in df.columns:
            return n
    return None


def funding_asof(funding: pl.DataFrame | None, close_ms: np.ndarray) -> np.ndarray:
    """Последняя известная ставка funding (FUNDING.ts <= close_ts)."""
    if funding is None or funding.is_empty() or "rate" not in funding.columns:
        return np.full(len(close_ms), np.nan)
    f = _norm_ts(funding).sort("ts")
    return asof_values(ms(f["ts"]), f["rate"].cast(pl.Float64).to_numpy(), close_ms)


OI_COLUMNS = ("oi_value", "sum_open_interest_value", "open_interest_value", "oi", "sum_open_interest",
              "open_interest")


def oi_change_pct(metrics: pl.DataFrame | None, close_ms: np.ndarray, lookback_ms: int) -> np.ndarray:
    """Изменение OI (%) за lookback к моменту close_ts. Колонка времени ts|create_time,
    OI — первая найденная из OI_COLUMNS (# ASSUMPTION: схема metrics — у слоя данных)."""
    n = len(close_ms)
    if metrics is None or metrics.is_empty():
        return np.full(n, np.nan)
    tcol = _pick(metrics, ("ts", "create_time"))
    ocol = _pick(metrics, OI_COLUMNS)
    if tcol is None or ocol is None:
        return np.full(n, np.nan)
    m = _norm_ts(metrics.select(tcol, ocol).rename({tcol: "ts"})).sort("ts")
    t = ms(m["ts"])
    oi = m[ocol].cast(pl.Float64).to_numpy()
    now = asof_values(t, oi, close_ms)
    then = asof_values(t, oi, close_ms - lookback_ms)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(then > 0, (now / then - 1.0) * 100.0, np.nan)


def btc_trend_asof(btc: Any, close_ms: np.ndarray) -> np.ndarray:
    """Тренд BTC (-1/0/1, NaN — неизвестно): число или кадр (close_ts|ts, btc_trend|htf_trend|trend),
    где время — момент, когда значение известно."""
    n = len(close_ms)
    if btc is None:
        return np.full(n, np.nan)
    if isinstance(btc, (int, float, np.integer, np.floating)):
        return np.full(n, float(btc))
    if isinstance(btc, pl.DataFrame) and not btc.is_empty():
        tcol = _pick(btc, ("close_ts", "ts"))
        vcol = _pick(btc, ("btc_trend", "htf_trend", "trend"))
        if tcol and vcol:
            b = _norm_ts(btc.select(tcol, vcol).rename({tcol: "ts"})).sort("ts")
            return asof_values(ms(b["ts"]), b[vcol].cast(pl.Float64).to_numpy(), close_ms)
    return np.full(n, np.nan)


# ---------------------------------------------------------------- сборка кадра


def assemble(df: pl.DataFrame, live: np.ndarray, cols: dict[str, np.ndarray]) -> pl.DataFrame:
    """Кадр оценок на все бары df: значения по live-барам; в halted-строках signal_* = False,
    остальное — null."""
    n = df.height
    dead = np.flatnonzero(~live)
    series = [df["ts"], df["close_ts"], pl.Series("halted", ~live)]
    for name, arr in cols.items():
        arr = np.asarray(arr)
        if arr.dtype == np.bool_ or arr.dtype.kind in "iu":
            full = np.zeros(n, dtype=arr.dtype)
            full[live] = arr
            s = pl.Series(name, full)
            if len(dead) and not name.startswith("signal_"):
                s = s.scatter(dead, None)
            series.append(s)
        else:
            full = np.full(n, np.nan)
            full[live] = arr
            series.append(pl.Series(name, full).fill_nan(None))
    return pl.DataFrame(series)


class NullSafeSignals:
    """Обход бага фундамента: Strategy.signals_from без сигналов возвращает
    conform(pl.DataFrame(), SIGNALS) — это ОДНА строка из null (литералы на пустом кадре), и on_bar
    превращает её в фантомный short-сигнал. Строки без side отбрасываются. Когда base/schemas
    исправят (conform на пустом кадре → 0 строк), фильтр станет no-op."""

    def signals_from(self, frame: pl.DataFrame, *, hh_symbol: str, tf: str) -> pl.DataFrame:
        out = super().signals_from(frame, hh_symbol=hh_symbol, tf=tf)  # type: ignore[misc]
        return out.filter(pl.col("side").is_not_null())


def edge(cond: np.ndarray) -> np.ndarray:
    """Срабатывание только на первом баре выполнения условия (без состояния, окно в 2 бара)."""
    return cond & ~core.shift(cond, 1)
