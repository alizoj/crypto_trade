"""Фикстуры стратегий: синтетические 1m-бары (случайное блуждание с режимами тренда) и внешние ряды."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest
from strat_helpers import DAYS, ts_series

from quant.data.aggregate import resample
from quant.indicators.synthetic import random_walk_bars


@pytest.fixture(scope="session")
def bars_1m() -> pl.DataFrame:
    # seed подобран так, чтобы в выборке были и бычьи, и медвежьи режимы HTF (сигналы в обе стороны)
    return random_walk_bars(DAYS * 1440, tf="1m", start="2024-03-01", seed=27, sigma_1m=0.0008, trend=0.04,
                            regime_bars=3000)


@pytest.fixture(scope="session")
def bars_1h(bars_1m) -> pl.DataFrame:
    return resample(bars_1m, "1h")


@pytest.fixture(scope="session")
def bars_4h(bars_1m) -> pl.DataFrame:
    return resample(bars_1m, "4h")


@pytest.fixture(scope="session")
def meta(bars_1m) -> dict:
    """funding (8h), metrics (OI, 5m) и тренд BTC (1h) — синтетика со временем «когда известно»."""
    rng = np.random.default_rng(5)
    t0 = int(bars_1m["ts"].dt.epoch("ms")[0])
    t1 = int(bars_1m["ts"].dt.epoch("ms")[-1])
    f_ts = np.arange(t0, t1, 8 * 3_600_000, dtype=np.int64)
    funding = pl.DataFrame({"ts": ts_series(f_ts), "rate": rng.normal(0.0001, 0.0004, len(f_ts)),
                            "mark_price": np.full(len(f_ts), 100.0), "source": ["binance_um"] * len(f_ts)})
    m_ts = np.arange(t0, t1, 5 * 60_000, dtype=np.int64)
    oi = 1e6 * np.exp(np.cumsum(rng.normal(0, 0.004, len(m_ts))))
    metrics = pl.DataFrame({"create_time": ts_series(m_ts), "sum_open_interest_value": oi})
    b_ts = np.arange(t0 + 3_600_000, t1, 3_600_000, dtype=np.int64)
    btc = pl.DataFrame({"close_ts": ts_series(b_ts), "btc_trend": rng.integers(-1, 2, len(b_ts))})
    return {"funding": funding, "metrics": metrics, "btc_trend": btc}
