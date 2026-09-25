"""Сходимость рекурсивных индикаторов при разных стартовых барах (раздел 6 ТЗ).

TradingView считает с первого бара своей истории, мы — со своего. После seed разность двух
рядов RMA/EMA убывает строго как (1 - alpha)^k. Для RMA(14): (13/14)^140 ≈ 3·10⁻⁵ — прогрева
10×period НЕ хватает для относительного допуска 1e-6; (13/14)^350 ≈ 6·10⁻¹² — 25×period хватает.
"""

from __future__ import annotations

import numpy as np
import pytest

from quant.indicators import adx, atr, ema, rma, rsi


def _series(n: int = 2000, start: int = 300, seed: int = 1) -> np.ndarray:
    """Положительный ряд около 1 с высоким уровнем в начале: разные старты дают большую разность seed."""
    rng = np.random.default_rng(seed)
    x = 1.0 + 0.5 * rng.random(n)
    x[:start] += 9.0
    return x


@pytest.mark.parametrize("n", [14, 20])
def test_rma_difference_decays_exactly(n):
    x, s = _series(), 300
    full = rma(x, n)
    late = np.full(len(x), np.nan)
    late[s:] = rma(x[s:], n)
    k0 = s + n - 1                           # бар seed позднего старта
    d0 = full[k0] - late[k0]
    alpha = 1.0 / n
    for k in (1, 10, 50, 100):
        assert full[k0 + k] - late[k0 + k] == pytest.approx(d0 * (1 - alpha) ** k, rel=1e-6)


def test_warmup_10x_not_enough_25x_enough():
    n, s = 14, 300
    x = _series(start=s)
    full = rma(x, n)
    late = np.full(len(x), np.nan)
    late[s:] = rma(x[s:], n)
    k0 = s + n - 1
    rel = np.abs(full - late) / np.abs(full)
    assert rel[k0 + 10 * n] > 1e-6               # 10×period: расхождение ~1e-4
    assert rel[k0 + 25 * n] < 1e-6               # 25×period: < 1e-6
    assert (13 / 14) ** 140 == pytest.approx(3.1e-5, rel=0.05)


@pytest.mark.parametrize("n", [20, 200])
def test_ema_warmup_25x(n):
    x = _series(n=30 * n + 400, start=300)
    s = 300
    full = ema(x, n)
    late = np.full(len(x), np.nan)
    late[s:] = ema(x[s:], n)
    k = s + n - 1 + 25 * n
    assert abs(full[k] - late[k]) / abs(full[k]) < 1e-6


def test_rsi_atr_adx_converge_after_25x(ohlcv):
    """На случайном блуждании: старт с бара 0 и с бара 200 совпадают до 1e-6 через 25×period."""
    h, lo, c = ohlcv["high"], ohlcv["low"], ohlcv["close"]
    s, n = 200, 14
    checks = {
        "rsi": (rsi(c, n), rsi(c[s:], n)),
        "atr": (atr(h, lo, c, n), atr(h[s:], lo[s:], c[s:], n)),
        "adx": (adx(h, lo, c, n, n), adx(h[s:], lo[s:], c[s:], n, n)),
    }
    for name, (full, late) in checks.items():
        k = 2 * n + 25 * n                        # ADX: два каскада RMA
        rel = np.abs(full[s + k:] - late[k:]) / np.abs(full[s + k:])
        assert np.nanmax(rel) < 1e-6, name
