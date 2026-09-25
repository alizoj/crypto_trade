"""Polars-обёртка add_indicators: колонки, halted-бары не участвуют в расчёте."""

from __future__ import annotations

import numpy as np
import polars as pl

from quant.indicators import rsi
from quant.indicators.frame import add_indicators
from quant.indicators.synthetic import random_walk_bars


def test_add_indicators_columns_and_halted():
    df = random_walk_bars(600, tf="1h", seed=3)
    out = add_indicators(df)
    for col in ("ema20", "ema50", "ema200", "rsi14", "atr14", "atr_pct", "plus_di", "minus_di", "adx",
                "bb_basis", "bb_upper", "bb_lower", "obv", "vwap", "vwap_dist_pct", "rvol", "cvd"):
        assert col in out.columns, col
    np.testing.assert_allclose(out["rsi14"].to_numpy()[14:], rsi(df["close"].to_numpy(), 14)[14:], rtol=1e-12)

    halted = np.zeros(600, dtype=bool)
    halted[300:320] = True
    dfh = df.with_columns(pl.Series("halted", halted))
    outh = add_indicators(dfh)
    assert outh.filter(pl.col("halted"))["rsi14"].null_count() == 20
    # на остальных барах — как если бы halted-баров не было вовсе
    ref = add_indicators(df.filter(~pl.Series(halted)))
    np.testing.assert_allclose(outh.filter(~pl.col("halted"))["ema50"].to_numpy(),
                               ref["ema50"].to_numpy(), rtol=1e-12, equal_nan=True)
