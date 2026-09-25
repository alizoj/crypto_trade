"""Набор признаков бара для исследования точек входа (только по закрытым барам, без look-ahead).

Один и тот же расчёт используют анализ «лучших входов» (разметка истории) и стратегия feature_rules,
которая проверяет выведенную гипотезу, — так правило в бэктесте совпадает с найденным при анализе.
Расстояния и доходности — в ATR сигнального ТФ, чтобы признаки были сравнимы между инструментами.
"""

from __future__ import annotations

import operator
from typing import Any

import numpy as np
import polars as pl

from quant.indicators import core
from quant.indicators.pivots import pivothigh, pivotlow
from quant.indicators.structure import market_structure
from quant.strategies.common import (
    bar_flags, btc_trend_asof, funding_asof, htf_block, htf_frame, indicator_block, ms, oi_change_pct,
    prepare_bars, prop_boundary_min,
)

FEATURES = (
    "rsi14", "rsi_chg3", "atr_pct", "adx", "di_diff", "vwap_dist_atr", "rvol", "cvd_ratio",
    "htf_trend", "htf_ema_dist_pct", "struct_trend", "struct_hh_hl",
    "ema20_dist_atr", "ema50_dist_atr", "ema200_dist_atr", "ema50_slope_atr",
    "ret1_atr", "ret4_atr", "ret24_atr", "ret72_atr", "pos24", "pos168",
    "bb_pctb", "bb_width_pct", "body_ratio", "upper_wick", "lower_wick", "range_atr",
    "pin_bull", "pin_bear", "ew_w2_long", "ew_w4_long", "ew_w2_short", "ew_w4_short",
    "funding_rate", "oi_chg24_pct", "btc_trend", "hour", "dow",
)
OPS = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge, "==": operator.eq}


def check_rules(rules: list, where: str = "rules") -> None:
    for r in rules or []:
        if len(r) != 3 or r[0] not in FEATURES or r[1] not in OPS:
            raise ValueError(f"{where}: правило {r!r}; признаки {FEATURES}, операторы {sorted(OPS)}")


def rules_mask(f: dict[str, np.ndarray], rules: list, n: int) -> np.ndarray:
    """Все правила [признак, оператор, порог] через И; пустой список — ни одного бара (сторона выключена)."""
    if not rules:
        return np.zeros(n, bool)
    hit = np.ones(n, bool)
    with np.errstate(invalid="ignore"):
        for feat, op, val in rules:
            hit &= OPS[op](f[feat], float(val))
    return hit


DEFAULTS: dict[str, Any] = {"ema_htf_len": 200, "htf_swing_left": 3, "htf_swing_right": 3, "atr_len": 14,
                            "vwap_anchor": "utc_day", "pin_wick": 0.6, "pin_body": 0.35, "pin_min_atr": 0.5,
                            "ew_left": 5, "ew_right": 2, "w1_min_atr": 2.0, "w2_fib": [0.382, 0.786],
                            "w4_fib": [0.236, 0.5], "w3_min_ratio": 1.0}


def bar_features(bars_tf: pl.DataFrame, *, tf: str, htf_tf: str = "4h", htf: pl.DataFrame | None = None,
                 bars_1m: pl.DataFrame | None = None, meta: dict[str, Any] | None = None,
                 params: dict[str, Any] | None = None) -> tuple[pl.DataFrame, np.ndarray, dict[str, np.ndarray]]:
    """(prepared df, live mask, {признак: массив по live-барам}) — массивы выровнены по live-барам df."""
    from quant.strategies.rsi_pin_ew import _elliott_nb

    p = {**DEFAULTS, **(params or {})}
    meta = meta or {}
    inst = meta.get("instrument")
    df = prepare_bars(bars_tf, tf, bars_1m)
    if df.is_empty():
        return df, np.zeros(0, bool), {}
    halted, _ = bar_flags(df)
    live = ~halted
    sub = df.filter(pl.Series(live))
    o = sub["open"].cast(pl.Float64).to_numpy()
    h = sub["high"].cast(pl.Float64).to_numpy()
    lo = sub["low"].cast(pl.Float64).to_numpy()
    c = sub["close"].cast(pl.Float64).to_numpy()
    v = sub["volume"].cast(pl.Float64).fill_null(0.0).to_numpy()
    tb = (sub["taker_buy_volume"].cast(pl.Float64).fill_null(np.nan).to_numpy()
          if "taker_buy_volume" in sub.columns else None)
    ts_ms = ms(sub["ts"])
    close_ms = ms(sub["close_ts"])

    ind = indicator_block(h, lo, c, v, tb, ts_ms, p, prop_boundary_min(getattr(inst, "market", "crypto_tradfi")))
    atr = ind["atr"]
    hb = htf_block(htf_frame(htf, bars_1m, df, htf_tf), close_ms, int(p["ema_htf_len"]), int(p["htf_swing_left"]),
                   int(p["htf_swing_right"]))
    st = market_structure(h, lo, c, 5, 2)
    _, ph_idx = pivothigh(h, int(p["ew_left"]), int(p["ew_right"]))
    _, pl_idx = pivotlow(lo, int(p["ew_left"]), int(p["ew_right"]))
    w2l, w4l, w2s, w4s, _, _ = _elliott_nb(h, lo, ph_idx, pl_idx, atr, float(p["w1_min_atr"]), float(p["w2_fib"][0]),
                                           float(p["w2_fib"][1]), float(p["w4_fib"][0]), float(p["w4_fib"][1]),
                                           float(p["w3_min_ratio"]))
    f: dict[str, np.ndarray] = {}
    with np.errstate(invalid="ignore", divide="ignore"):
        r = ind["rsi14"]
        f["rsi14"] = r
        f["rsi_chg3"] = r - core.shift(r, 3)
        f["atr_pct"] = ind["atr_pct"]
        f["adx"] = ind["adx"]
        f["di_diff"] = ind["plus_di"] - ind["minus_di"]
        f["vwap_dist_atr"] = (c - ind["vwap"]) / atr
        f["rvol"] = ind["rvol"]
        f["cvd_ratio"] = ind["cvd_ratio"]
        f["htf_trend"] = hb["htf_trend"].astype(np.float64)
        f["htf_ema_dist_pct"] = (hb["htf_close"] / hb["htf_ema"] - 1.0) * 100.0
        f["struct_trend"] = st.trend.astype(np.float64)
        f["struct_hh_hl"] = st.hh_hl.astype(np.float64)
        for n in (20, 50, 200):
            f[f"ema{n}_dist_atr"] = (c - core.ema(c, n)) / atr
        e50 = core.ema(c, 50)
        f["ema50_slope_atr"] = (e50 - core.shift(e50, 10)) / atr
        for n in (1, 4, 24, 72):
            f[f"ret{n}_atr"] = (c - core.shift(c, n)) / atr
        for n, name in ((24, "pos24"), (168, "pos168")):
            hi, lw = core.highest(h, n), core.lowest(lo, n)
            f[name] = (c - lw) / (hi - lw)
        mid, up, dn = core.bb(c, 20, 2.0)
        f["bb_pctb"] = (c - dn) / (up - dn)
        f["bb_width_pct"] = (up - dn) / mid * 100.0
        rng = h - lo
        f["body_ratio"] = np.where(rng > 0, (c - o) / rng, 0.0)
        f["upper_wick"] = np.where(rng > 0, (h - np.maximum(o, c)) / rng, 0.0)
        f["lower_wick"] = np.where(rng > 0, (np.minimum(o, c) - lo) / rng, 0.0)
        f["range_atr"] = rng / atr
        pin_ok = (rng > 0) & (rng >= float(p["pin_min_atr"]) * atr) & (np.abs(c - o) <= float(p["pin_body"]) * rng)
        f["pin_bull"] = (pin_ok & (f["lower_wick"] >= float(p["pin_wick"]))).astype(np.float64)
        f["pin_bear"] = (pin_ok & (f["upper_wick"] >= float(p["pin_wick"]))).astype(np.float64)
        f["ew_w2_long"], f["ew_w4_long"] = w2l.astype(np.float64), w4l.astype(np.float64)
        f["ew_w2_short"], f["ew_w4_short"] = w2s.astype(np.float64), w4s.astype(np.float64)
        f["funding_rate"] = funding_asof(meta.get("funding"), close_ms)
        f["oi_chg24_pct"] = oi_change_pct(meta.get("metrics"), close_ms, 24 * 3_600_000)
        f["btc_trend"] = btc_trend_asof(meta.get("btc_trend"), close_ms)
        f["hour"] = ((close_ms // 3_600_000) % 24).astype(np.float64)
        f["dow"] = (((close_ms // 86_400_000) + 3) % 7).astype(np.float64)      # 0 = понедельник
    f["_atr"] = atr
    f["_close"] = c
    f["_close_ms"] = close_ms.astype(np.float64)
    return df, live, f

