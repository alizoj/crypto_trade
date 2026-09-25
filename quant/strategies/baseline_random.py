"""baseline_random — случайные входы, эталон P(pass) (раздел 6 ТЗ).

Стратегия полезна, только если её P(pass both) статистически значимо выше baseline.

Вход на баре — с вероятностью signals_per_day · tf_min / 1440, шорт с вероятностью short_share
(по умолчанию 50/50; иначе — эталон с тем же перекосом по сторонам, что у проверяемой стратегии), score = 50.
SL = sl_atr · ATR(atr_len), TP = rr · SL-дистанция (один тейк).
Случайность — детерминированный хэш (seed, hh_symbol, close_ts), а не поток ГСЧ: решение на баре
не зависит ни от окна данных, ни от будущих баров (паритет on_bar/compute, нет look-ahead).
Общие правила (halted, synthetic, сессия TradFi) — как у всех стратегий.
"""

from __future__ import annotations

import zlib
from typing import Any

import numpy as np
import polars as pl

from quant.schemas import TS
from quant.strategies.base import Factor, Side, Strategy
from quant.strategies.common import (
    NullSafeSignals, assemble, bar_flags, deep_merge, htf_block, htf_frame, indicator_block, ms, prepare_bars,
    prop_boundary_min, session_ok,
)
from quant.timeutil import tf_minutes

DEFAULTS: dict[str, Any] = {
    "tf": "1h",
    "htf": "4h",
    "signals_per_day": 1.0,
    "short_share": 0.5,
    "seed": 42,
    "atr_len": 14,
    "sl_atr": 1.5,
    "rr": 1.0,
    "score": 50.0,
    "ema_htf_len": 200,          # только для колонки скринера htf_trend
    "htf_swing_left": 3,
    "htf_swing_right": 3,
    "vwap_anchor": "utc_day",
    "max_hold_min": 1440,
    "valid_min": 60,
    "entry_type": "market",
    "tp1_frac": 1.0,
    "sl_trigger": "last",
    "filters": {"tradfi_off_session_signals": False},
}

_M64 = np.uint64(0xFFFFFFFFFFFFFFFF)


def _splitmix64(x: np.ndarray) -> np.ndarray:
    z = (x + np.uint64(0x9E3779B97F4A7C15)) & _M64
    z = ((z ^ (z >> np.uint64(30))) * np.uint64(0xBF58476D1CE4E5B9)) & _M64
    z = ((z ^ (z >> np.uint64(27))) * np.uint64(0x94D049BB133111EB)) & _M64
    return z ^ (z >> np.uint64(31))


def hash_uniform(seed: int, key: str, ts_ms: np.ndarray, stream: int = 0) -> np.ndarray:
    """Равномерное [0, 1) из (seed, key, ts, stream) — стабильно между процессами."""
    salt = (zlib.crc32(key.encode()) << 32) ^ (int(seed) * 0x9E3779B1 + stream * 0x85EBCA77)
    x = np.asarray(ts_ms, dtype=np.int64).astype(np.uint64) ^ np.uint64(salt & 0xFFFFFFFFFFFFFFFF)
    z = _splitmix64(_splitmix64(x))
    return (z >> np.uint64(11)).astype(np.float64) / float(1 << 53)


class BaselineRandom(NullSafeSignals, Strategy):
    name = "baseline_random"

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(deep_merge(DEFAULTS, params))

    def warmup_bars(self) -> int:
        return 25 * int(self.params["atr_len"])

    def compute(self, bars_tf: pl.DataFrame, *, hh_symbol: str, tf: str, htf: pl.DataFrame | None = None,
                bars_1m: pl.DataFrame | None = None, meta: dict[str, Any] | None = None) -> pl.DataFrame:
        p = self.params
        meta = meta or {}
        inst = meta.get("instrument")
        df = prepare_bars(bars_tf, tf, bars_1m)
        if df.is_empty():
            return self._empty()
        halted, blocked_all = bar_flags(df)
        live = ~halted
        sub = df.filter(pl.Series(live))
        blocked = blocked_all[live]
        h = sub["high"].cast(pl.Float64).to_numpy()
        lo = sub["low"].cast(pl.Float64).to_numpy()
        c = sub["close"].cast(pl.Float64).to_numpy()
        v = sub["volume"].cast(pl.Float64).fill_null(0.0).to_numpy()
        tb = (sub["taker_buy_volume"].cast(pl.Float64).fill_null(np.nan).to_numpy()
              if "taker_buy_volume" in sub.columns else None)
        ts_ms = ms(sub["ts"])
        close_ms = ms(sub["close_ts"])
        n = len(c)

        ind = indicator_block(h, lo, c, v, tb, ts_ms, p, prop_boundary_min(getattr(inst, "market", "crypto_tradfi")))
        hb = htf_block(htf_frame(htf, bars_1m, df, p["htf"]), close_ms, int(p["ema_htf_len"]),
                       int(p["htf_swing_left"]), int(p["htf_swing_right"]))
        atr = ind["atr"]
        prob = min(1.0, float(p["signals_per_day"]) * tf_minutes(tf) / 1440.0)
        u_fire = hash_uniform(int(p["seed"]), hh_symbol, close_ms, 0)
        u_side = hash_uniform(int(p["seed"]), hh_symbol, close_ms, 1)
        with np.errstate(invalid="ignore"):
            ready = ~np.isnan(atr) & (atr > 0)
        ok = ready & ~blocked & session_ok(close_ms, inst, p.get("filters") or {})
        fire = ok & (u_fire < prob)
        is_long = u_side < 1.0 - float(p["short_share"])
        sig_long = fire & is_long
        sig_short = fire & ~is_long
        k, rr = float(p["sl_atr"]), float(p["rr"])
        score = np.where(ready, float(p["score"]), np.nan)
        cols: dict[str, np.ndarray] = {
            "close": c,
            "score_long": score, "score_short": score,
            "signal_long": sig_long, "signal_short": sig_short,
            "entry_long": np.where(ready, c, np.nan), "sl_long": c - k * atr, "tp1_long": c + rr * k * atr,
            "tp2_long": np.full(n, np.nan),
            "entry_short": np.where(ready, c, np.nan), "sl_short": c + k * atr, "tp1_short": c - rr * k * atr,
            "tp2_short": np.full(n, np.nan),
            "c_random_long": score, "c_random_short": score,
            "rsi14": ind["rsi14"], "adx": ind["adx"], "atr_pct": ind["atr_pct"],
            "vwap_dist_pct": ind["vwap_dist_pct"], "htf_trend": hb["htf_trend"], "cvd_trend": ind["cvd_trend"],
            "atr": atr, "blocked": blocked,
        }
        return assemble(df, live, cols)

    def _empty(self) -> pl.DataFrame:
        schema: dict[str, Any] = {"ts": TS, "close_ts": TS, "halted": pl.Boolean, "close": pl.Float64,
                                  "score_long": pl.Float64, "score_short": pl.Float64,
                                  "signal_long": pl.Boolean, "signal_short": pl.Boolean}
        for side in ("long", "short"):
            for k in ("entry", "sl", "tp1", "tp2"):
                schema[f"{k}_{side}"] = pl.Float64
        return pl.DataFrame(schema=schema)

    def explain(self, row: dict[str, Any], side: Side) -> list[Factor]:
        return [Factor("random", f"Случайный вход (baseline, seed {self.params['seed']})", None,
                       float(self.params["score"]), 100.0, "neutral")]
