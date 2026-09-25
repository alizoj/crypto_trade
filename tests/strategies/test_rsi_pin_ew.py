"""rsi_pin_ew: формализация волн Эллиотта на заданных свингах, контроль со случайными входами."""

from __future__ import annotations

import numpy as np
from strat_helpers import RSI_PIN_EW_PARAMS, SYMBOL

from quant.strategies import get_strategy
from quant.strategies.rsi_pin_ew import _elliott_nb

ARGS = (2.0, 0.382, 0.786, 0.236, 0.5, 1.0)     # w1_min_atr, w2_fib, w4_fib, w3_min_ratio


def _run(low, high, pivots_low, pivots_high):
    """pivots_*: {бар подтверждения: бар пивота}."""
    n = len(low)
    pl_idx = np.full(n, -1, np.int64)
    ph_idx = np.full(n, -1, np.int64)
    for i, p in pivots_low.items():
        pl_idx[i] = p
    for i, p in pivots_high.items():
        ph_idx[i] = p
    return _elliott_nb(np.asarray(high, float), np.asarray(low, float), ph_idx, pl_idx, np.ones(n), *ARGS)


def test_wave2_setup_and_invalidation():
    low, high = np.full(20, 108.0), np.full(20, 109.0)
    low[2], high[8] = 100.0, 110.0                    # L0 = 100, H1 = 110: волна 1 = 10 ATR
    low[11], low[12], low[13] = 104.0, 99.5, 104.0    # откат 60% → сетап; затем ниже L0 → волна 2 сломана
    w2l, w4l, w2s, w4s, ext_l, _ = _run(low, high, {4: 2}, {10: 8})
    assert not w2l[10]                                # откат 20% — мелко
    assert w2l[11] and ext_l[11] == 104.0             # опора стопа — минимум отката
    assert not w2l[12] and not w2l[13]                # минимум после H1 ниже L0 — не волна 2
    assert not w4l.any() and not w2s.any() and not w4s.any()


def test_wave4_setup_and_overlap_rule():
    low, high = np.full(30, 124.0), np.full(30, 125.0)
    low[2], high[8], low[14], high[20] = 100.0, 110.0, 105.0, 125.0   # 1: 100→110, 2: →105, 3: →125
    low[23], low[24], low[25] = 120.0, 109.0, 120.0
    w2l, w4l, *_ = _run(low, high, {4: 2, 16: 14}, {10: 8, 22: 20})
    assert w4l[23] and not w2l[23]                    # откат волны 3 на 25% — вход в волну 5
    assert not w4l[24] and not w4l[25]                # волна 4 зашла ниже вершины волны 1


def test_random_control_keeps_levels(bars_1h, bars_4h):
    setup = get_strategy("rsi_pin_ew", RSI_PIN_EW_PARAMS).compute(bars_1h, hh_symbol=SYMBOL, tf="1h", htf=bars_4h)
    rnd = get_strategy("rsi_pin_ew", {**RSI_PIN_EW_PARAMS, "entry_mode": "random", "random_signals_per_day": 2.0,
                                      "random_short_share": 0.8}).compute(bars_1h, hh_symbol=SYMBOL, tf="1h",
                                                                          htf=bars_4h)
    for col in ("sl_long", "tp1_long", "tp2_long", "sl_short"):
        assert setup[col].equals(rnd[col])            # те же уровни — отличаются только моменты входа
    n_long, n_short = int(rnd["signal_long"].sum()), int(rnd["signal_short"].sum())
    assert n_long + n_short > 3 * int(setup["signal_long"].sum() + setup["signal_short"].sum())
    assert 0.65 < n_short / (n_long + n_short) < 0.95
