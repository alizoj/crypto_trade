"""Индикаторы Элдера: ручной пересчёт на коротких рядах."""

import numpy as np

from quant.indicators import core, elder


def test_force_index():
    c = np.array([10.0, 11.0, 10.5, 10.5])
    v = np.array([100.0, 200.0, 300.0, 400.0])
    fi = elder.force_index(c, v)
    assert np.isnan(fi[0])
    np.testing.assert_allclose(fi[1:], [200.0, -150.0, 0.0])


def test_macd_hist_matches_definition():
    rng = np.random.default_rng(0)
    c = 100 + np.cumsum(rng.normal(0, 1, 300))
    line = core.ema(c, 12) - core.ema(c, 26)
    np.testing.assert_allclose(elder.macd_hist(c), line - core.ema(line, 9), equal_nan=True)


def test_impulse_colors():
    flat = np.full(150, 100.0)
    up = np.concatenate([flat, 100.0 + np.arange(1, 11)])     # начало подъёма: EMA и гистограмма растут
    dn = np.concatenate([flat, 100.0 - np.arange(1, 11)])
    assert elder.impulse(up)[-1] == 1
    assert elder.impulse(dn)[-1] == -1
    assert elder.impulse(flat)[-1] == 0
    assert elder.impulse(up).dtype == np.int8


def test_thermometer():
    h = np.array([10.0, 11.0, 10.5, 12.0])
    lo = np.array([9.0, 9.5, 9.8, 8.0])
    t = elder.thermometer(h, lo)
    # бар 1: вверх на 1.0; бар 2 — внутренний (0); бар 3: max(12-10.5, 9.8-8) = 1.8
    np.testing.assert_allclose(t[1:], [1.0, 0.0, 1.8])


def test_safezone_long_stop():
    lo = np.array([10.0, 9.0, 9.5, 8.5, 9.0])
    h = lo + 1.0
    sl_long, sl_short = elder.safezone_stops(h, lo, lookback=4, k=2.0)
    # прорывы вниз за 4 бара до бара 4: 1.0 (бар 1), 1.0 (бар 3) → средний 1.0; стоп = L[4] − 2 × 1.0
    assert sl_long[4] == 9.0 - 2.0 * 1.0
    # прорывы вверх (H − H[1] > 0): бар 2 (+0.5) и бар 4 (+0.5) → средний 0.5; стоп шорта = H[4] + 2 × 0.5
    assert sl_short[4] == 10.0 + 2.0 * 0.5
    assert np.isnan(sl_long[2])                         # окно ещё неполное
