"""Polars-обёртка: добавить стандартный набор индикаторов к кадру баров ТФ.

Бары с halted=True индикаторы не получают (раздел 3.2.2): расчёт идёт по остальным барам,
в halted-строках — null.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from quant.indicators import core, volume as vol


def ts_ms(df: pl.DataFrame, col: str = "ts") -> np.ndarray:
    return df[col].dt.epoch("ms").to_numpy().astype(np.int64)


def add_indicators(bars: pl.DataFrame, *, ema_lens: tuple[int, ...] = (20, 50, 200), rsi_len: int = 14,
                   atr_len: int = 14, adx_len: int = 14, bb_len: int = 20, bb_mult: float = 2.0,
                   rvol_len: int = 20, vwap_anchor: str = "utc_day",
                   boundary_min: int = vol.PROP_DAY_BOUNDARY_MIN) -> pl.DataFrame:
    """Колонки: ema<n>, rsi<len>, atr<len>, atr_pct, plus_di, minus_di, adx, bb_basis/upper/lower,
    obv, vwap, vwap_dist_pct, rvol, cvd (если есть taker_buy_volume)."""
    df = bars.sort("ts")
    n = df.height
    if n == 0:
        return df
    live = ~df["halted"].fill_null(False).to_numpy() if "halted" in df.columns else np.ones(n, dtype=bool)
    sub = df.filter(pl.Series(live))
    h = sub["high"].to_numpy().astype(np.float64)
    lo = sub["low"].to_numpy().astype(np.float64)
    c = sub["close"].to_numpy().astype(np.float64)
    v = sub["volume"].to_numpy().astype(np.float64)
    out: dict[str, np.ndarray] = {}
    for ln in ema_lens:
        out[f"ema{ln}"] = core.ema(c, ln)
    out[f"rsi{rsi_len}"] = core.rsi(c, rsi_len)
    a = core.atr(h, lo, c, atr_len)
    out[f"atr{atr_len}"] = a
    out["atr_pct"] = a / c * 100.0
    pdi, mdi, adx = core.dmi(h, lo, c, adx_len, adx_len)
    out["plus_di"], out["minus_di"], out["adx"] = pdi, mdi, adx
    out["bb_basis"], out["bb_upper"], out["bb_lower"] = core.bb(c, bb_len, bb_mult)
    out["obv"] = core.obv(c, v)
    key = vol.session_key(ts_ms(sub), vwap_anchor, boundary_min)
    vw = vol.vwap(core.hlc3(h, lo, c), v, key)
    out["vwap"] = vw
    out["vwap_dist_pct"] = (c - vw) / vw * 100.0
    out["rvol"] = vol.rvol(v, rvol_len)
    if "taker_buy_volume" in sub.columns:
        out["cvd"] = vol.cvd(v, sub["taker_buy_volume"].to_numpy().astype(np.float64))
    cols = []
    for name, arr in out.items():
        full = np.full(n, np.nan)
        full[live] = arr
        cols.append(pl.Series(name, full).fill_nan(None))
    return df.with_columns(cols)
