"""Приёмка фазы 3: нет look-ahead (раздел 0, 6, 9 ТЗ).

1. Сигналы на полных данных = сигналы на данных, обрезанных в случайной минуте (до точки среза),
   включая HTF (старший ТФ агрегируется из тех же обрезанных 1m — с незакрытым последним баром).
2. Искажение будущего (после точки среза) не меняет ни одной строки кадра оценок до неё.
Проверяются обе стратегии, с фильтрами и внешними рядами (funding, OI, тренд BTC).
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest
from polars.testing import assert_frame_equal
from strat_helpers import SYMBOL, cut_meta, make_strategy

from quant.data.aggregate import resample
from quant.indicators.synthetic import random_walk_bars

CASES = [("confluence_v1", False), ("confluence_v1", True), ("baseline_random", False), ("rsi_pin_ew", False), ("feature_rules", False)]
IDS = ["confluence", "confluence+filters", "baseline", "rsi_pin_ew", "feature_rules"]


def _cut_points(bars_1m: pl.DataFrame, k: int, seed: int) -> list[int]:
    ts = bars_1m["ts"].dt.epoch("ms").to_numpy()
    rng = np.random.default_rng(seed)
    lo, hi = ts[0] + 40 * 86_400_000, ts[-1] - 3_600_000
    pts = rng.integers(lo // 60_000, hi // 60_000, k) * 60_000       # случайные минуты, не по сетке ТФ
    return sorted(int(x) for x in pts)


def _same(part: pl.DataFrame, full: pl.DataFrame) -> None:
    ref = full.filter(pl.col("ts").is_in(part["ts"].implode()))
    assert part.height == ref.height
    assert_frame_equal(part, ref, check_exact=True)


@pytest.mark.parametrize("name,filters", CASES, ids=IDS)
def test_truncation_random_points(bars_1m, meta, name, filters):
    s = make_strategy(name, filters)
    full = s.compute(resample(bars_1m, "1h"), hh_symbol=SYMBOL, tf="1h", bars_1m=bars_1m, meta=meta)
    assert full["signal_long"].sum() > 5 and full["signal_short"].sum() > 5
    for t in _cut_points(bars_1m, 6, seed=1):
        b1m = bars_1m.filter(pl.col("ts").dt.epoch("ms") < t)
        b1h = resample(b1m, "1h")                                     # последний 1h-бар — незакрытый
        part = s.compute(b1h, hh_symbol=SYMBOL, tf="1h", bars_1m=b1m, meta=cut_meta(meta, t))
        assert part["close_ts"].dt.epoch("ms").max() <= t             # незакрытый бар отброшен
        assert part.height >= b1h.height - 1
        _same(part, full)


@pytest.mark.parametrize("name,filters", CASES, ids=IDS)
def test_truncation_tf_bars_with_explicit_htf(bars_1m, bars_1h, name, filters):
    """Без 1m: бары ТФ до k-го и HTF, агрегированный из данных до закрытия k-го бара."""
    s = make_strategy(name, filters)
    full = s.compute(bars_1h, hh_symbol=SYMBOL, tf="1h", htf=resample(bars_1m, "4h"))
    rng = np.random.default_rng(2)
    for k in sorted(rng.integers(1000, bars_1h.height - 5, 5)):
        b1h = bars_1h.head(int(k))
        t = int(b1h["ts"].dt.epoch("ms")[-1]) + 3_600_000
        htf = resample(bars_1m.filter(pl.col("ts").dt.epoch("ms") < t), "4h")   # с незакрытым 4h-баром
        part = s.compute(b1h, hh_symbol=SYMBOL, tf="1h", htf=htf)
        _same(part, full)


def _distort_after(bars_1m: pl.DataFrame, t: int, seed: int) -> pl.DataFrame:
    """Будущее после t — другое случайное блуждание (с того же уровня цены)."""
    past = bars_1m.filter(pl.col("ts").dt.epoch("ms") < t)
    fut_n = bars_1m.height - past.height
    alt = random_walk_bars(fut_n, tf="1m", start="2000-01-01", seed=seed, sigma_1m=0.004, trend=0.5,
                           price0=float(past["close"][-1]))
    alt = alt.with_columns(bars_1m["ts"].tail(fut_n).alias("ts"),
                           (pl.col("volume") * 7.0).alias("volume"))
    return pl.concat([past, alt.select(past.columns)])


@pytest.mark.parametrize("name,filters", CASES, ids=IDS)
def test_future_distortion_does_not_change_past(bars_1m, meta, name, filters):
    s = make_strategy(name, filters)
    full = s.compute(resample(bars_1m, "1h"), hh_symbol=SYMBOL, tf="1h", bars_1m=bars_1m, meta=meta)
    for i, t in enumerate(_cut_points(bars_1m, 3, seed=3)):
        d1m = _distort_after(bars_1m, t, seed=100 + i)
        d_meta = cut_meta(meta, t)
        dist = s.compute(resample(d1m, "1h"), hh_symbol=SYMBOL, tf="1h", bars_1m=d1m, meta=d_meta)
        past_full = full.filter(pl.col("close_ts").dt.epoch("ms") <= t)
        past_dist = dist.filter(pl.col("close_ts").dt.epoch("ms") <= t)
        assert_frame_equal(past_dist, past_full, check_exact=True)
        # будущее действительно изменилось
        fut = dist.filter(pl.col("close_ts").dt.epoch("ms") > t + 86_400_000)
        ref = full.filter(pl.col("ts").is_in(fut["ts"].implode()))
        assert not fut["close"].equals(ref["close"])
