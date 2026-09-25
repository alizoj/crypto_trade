"""Синтетические бары (случайное блуждание) для тестов и санити-чеков — пока нет реальных данных.

Колонки — как BARS_1M (quant.schemas) плюс mark-цена m_open..m_close. При trend=0 цена —
мартингал (лог-приращения с поправкой -σ²/2). trend>0 добавляет дрейф ±trend·σ за бар
режимами случайной длины (в среднем regime_bars баров) — чтобы у трендовых факторов было на чём
срабатывать в обе стороны.
"""

from __future__ import annotations

import numpy as np
import polars as pl

from quant.schemas import TS
from quant.timeutil import parse_dt, tf_minutes, to_ms


def random_walk_bars(n: int, *, tf: str = "1m", start: str = "2024-01-01", seed: int = 0,
                     price0: float = 100.0, sigma_1m: float = 0.0008, trend: float = 0.0,
                     regime_bars: int = 500, volume_mean_1m: float = 50.0,
                     mark_noise: float = 0.0002) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    step = tf_minutes(tf)
    sig = sigma_1m * np.sqrt(step)
    z = rng.standard_normal(n)
    mu = np.zeros(n)
    if trend:
        i = 0
        while i < n:
            ln = max(1, int(rng.exponential(regime_bars)))
            mu[i:i + ln] = trend * sig * rng.choice((-1.0, 1.0))
            i += ln
    r = mu - 0.5 * sig * sig + sig * z
    close = price0 * np.exp(np.cumsum(r))
    open_ = np.empty(n)
    open_[0] = price0
    open_[1:] = close[:-1]
    wick_hi = np.exp(np.abs(rng.normal(0.0, 0.5 * sig, n)))
    wick_lo = np.exp(-np.abs(rng.normal(0.0, 0.5 * sig, n)))
    high = np.maximum(open_, close) * wick_hi
    low = np.minimum(open_, close) * wick_lo
    volume = volume_mean_1m * step * rng.lognormal(0.0, 0.5, n) * (1.0 + 0.5 * np.abs(z))
    frac = np.clip(0.5 + 0.2 * np.tanh(z) + rng.normal(0.0, 0.05, n), 0.02, 0.98)
    taker = volume * frac
    typical = (high + low + close) / 3.0
    mk = 1.0 + rng.normal(0.0, mark_noise, (4, n))
    m_open, m_close = open_ * mk[0], close * mk[1]
    m_high = np.maximum(np.maximum(m_open, m_close), high * mk[2])
    m_low = np.minimum(np.minimum(m_open, m_close), low * mk[3])
    t0 = to_ms(parse_dt(start))
    ts = t0 + np.arange(n, dtype=np.int64) * step * 60_000
    return pl.DataFrame({
        "ts": pl.Series(ts).cast(pl.Datetime("ms")).dt.replace_time_zone("UTC").cast(TS),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "quote_volume": volume * typical,
        "trades": rng.poisson(20 * step, n).astype(np.int64),
        "taker_buy_volume": taker, "taker_buy_quote_volume": taker * typical,
        "halted": np.zeros(n, dtype=bool), "zero_vol": np.zeros(n, dtype=bool),
        "synthetic": np.zeros(n, dtype=bool),
        "version": pl.Series(np.full(n, 3, dtype=np.int8)),
        "source": ["binance_um"] * n,
        "m_open": m_open, "m_high": m_high, "m_low": m_low, "m_close": m_close,
    })
