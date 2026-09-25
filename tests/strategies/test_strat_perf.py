"""Производительность (приёмка фазы 3): compute confluence_v1 на 3 годах 1h-баров (~26k) — секунды."""

from __future__ import annotations

import time

import numpy as np
import polars as pl
from strat_helpers import ts_series

from quant.indicators.synthetic import random_walk_bars
from quant.strategies import get_strategy

LIMIT_SEC = 5.0


def test_confluence_3y_1h_under_seconds():
    bars = random_walk_bars(3 * 365 * 24, tf="1h", seed=9, trend=0.1, regime_bars=300)
    t0, t1 = int(bars["ts"].dt.epoch("ms")[0]), int(bars["ts"].dt.epoch("ms")[-1])
    f_ts = np.arange(t0, t1, 8 * 3_600_000, dtype=np.int64)
    m_ts = np.arange(t0, t1, 5 * 60_000, dtype=np.int64)
    meta = {
        "funding": pl.DataFrame({"ts": ts_series(f_ts), "rate": np.full(len(f_ts), 0.0001)}),
        "metrics": pl.DataFrame({"ts": ts_series(m_ts), "sum_open_interest_value": np.linspace(1e6, 2e6, len(m_ts))}),
    }
    s = get_strategy("confluence_v1", {"filters": {"adx_filter": True, "funding_oi_filter": True,
                                                   "cvd_divergence_filter": True}})
    s.compute(bars.head(3000), hh_symbol="X", tf="1h", meta=meta)          # JIT-компиляция numba
    t = time.perf_counter()
    fr = s.compute(bars, hh_symbol="X", tf="1h", meta=meta)
    dt = time.perf_counter() - t
    assert fr.height == bars.height >= 26_000
    assert fr["signal_long"].sum() + fr["signal_short"].sum() > 0
    assert dt < LIMIT_SEC, f"compute {dt:.2f} с на {bars.height} барах"
