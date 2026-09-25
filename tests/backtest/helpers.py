"""Синтетические данные для тестов бэктеста."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import polars as pl

from quant.schemas import SIGNALS, conform

T0 = datetime(2025, 1, 6, 0, 0, tzinfo=timezone.utc)


def bars_from_ohlc(rows: list[tuple[float, float, float, float]], *, start: datetime = T0,
                   halted: list[bool] | None = None, mark: list[tuple[float, float, float, float]] | None = None) -> pl.DataFrame:
    n = len(rows)
    ts = [int(start.timestamp() * 1000) + i * 60_000 for i in range(n)]
    o, h, l, c = (list(x) for x in zip(*rows))
    df = pl.DataFrame({
        "ts": pl.Series(ts, dtype=pl.Int64).cast(pl.Datetime("ms", "UTC")),
        "open": o, "high": h, "low": l, "close": c,
        "volume": [1.0] * n, "quote_volume": [1.0] * n, "trades": [1] * n,
        "taker_buy_volume": [0.5] * n, "taker_buy_quote_volume": [0.5] * n,
        "halted": halted or [False] * n, "zero_vol": [False] * n, "synthetic": [False] * n,
        "version": [3] * n, "source": ["binance_um"] * n,
    })
    if mark is not None:
        mo, mh, ml, mc = (list(x) for x in zip(*mark))
        df = df.with_columns(pl.Series("m_open", mo), pl.Series("m_high", mh), pl.Series("m_low", ml), pl.Series("m_close", mc))
    else:
        df = df.with_columns(pl.col("open").alias("m_open"), pl.col("high").alias("m_high"),
                             pl.col("low").alias("m_low"), pl.col("close").alias("m_close"))
    return df


def random_walk_bars(n_min: int, *, seed: int = 1, start: datetime = T0, price: float = 100.0,
                     sigma: float = 0.0005, substeps: int = 8) -> pl.DataFrame:
    """Мартингал: лог-случайное блуждание без сноса, OHLC из подшагов внутри минуты."""
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, sigma / np.sqrt(substeps), size=(n_min, substeps))
    path = np.log(price) + np.cumsum(steps.ravel())
    path = np.exp(path).reshape(n_min, substeps)
    opens = np.concatenate([[price], path[:-1, -1]])
    highs = np.maximum(path.max(axis=1), opens)
    lows = np.minimum(path.min(axis=1), opens)
    closes = path[:, -1]
    ts = (np.int64(start.timestamp() * 1000) + np.arange(n_min, dtype=np.int64) * 60_000)
    df = pl.DataFrame({
        "ts": pl.Series(ts).cast(pl.Datetime("ms", "UTC")),
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": np.ones(n_min), "quote_volume": closes, "trades": np.ones(n_min, dtype=np.int64),
        "taker_buy_volume": np.full(n_min, 0.5), "taker_buy_quote_volume": closes / 2,
        "halted": np.zeros(n_min, dtype=bool), "zero_vol": np.zeros(n_min, dtype=bool),
        "synthetic": np.zeros(n_min, dtype=bool), "version": np.full(n_min, 3, dtype=np.int8),
        "source": ["synthetic"] * n_min,
    })
    return df.with_columns(pl.col("open").alias("m_open"), pl.col("high").alias("m_high"),
                           pl.col("low").alias("m_low"), pl.col("close").alias("m_close"))


def signals_df(rows: list[dict], symbol: str = "BTCUSDT", tf: str = "1h") -> pl.DataFrame:
    base = {"hh_symbol": symbol, "tf": tf, "entry_type": "market", "tp2": None, "tp1_frac": 1.0,
            "score": 50.0, "max_hold_min": 1440, "valid_min": 60}
    data = [{**base, **r} for r in rows]
    df = pl.DataFrame(data)
    return conform(df, SIGNALS)
