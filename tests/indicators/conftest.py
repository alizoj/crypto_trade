"""Фикстуры индикаторов: синтетические 1h-бары (реальных данных пока нет)."""

from __future__ import annotations

import numpy as np
import pytest

from quant.indicators.synthetic import random_walk_bars


@pytest.fixture(scope="session")
def ohlcv() -> dict[str, np.ndarray]:
    df = random_walk_bars(1500, tf="1h", seed=7, trend=0.1, regime_bars=200)
    out = {k: df[k].to_numpy().astype(np.float64) for k in
           ("open", "high", "low", "close", "volume", "taker_buy_volume")}
    out["ts"] = df["ts"].dt.epoch("ms").to_numpy().astype(np.int64)
    return out
