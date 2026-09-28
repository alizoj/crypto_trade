"""Индикаторы А. Элдера («Трейдинг с доктором Элдером», гл. 5–6): индекс силы, гистограмма MACD,
импульсная система, рыночный термометр, защитные стопы SafeZone.

Всё — по закрытым барам, значение на баре i известно на его закрытии.
"""

from __future__ import annotations

import numpy as np

from quant.indicators import core


def force_index(close, volume) -> np.ndarray:
    """Индекс силы: (C − C[1]) × V."""
    c = core.f64(close)
    return (c - core.shift(c, 1)) * core.f64(volume)


def macd_hist(close, fast: int = 12, slow: int = 26, signal: int = 9) -> np.ndarray:
    """Гистограмма MACD: (EMA fast − EMA slow) − EMA signal от этой разности."""
    c = core.f64(close)
    line = core.ema(c, fast) - core.ema(c, slow)
    return line - core.ema(line, signal)


def impulse(close, ema_len: int = 13) -> np.ndarray:
    """Импульсная система: +1 — EMA и гистограмма MACD растут («зелёный» бар), −1 — обе падают, иначе 0."""
    e = core.ema(close, ema_len)
    h = macd_hist(close)
    de, dh = core.change(e, 1), core.change(h, 1)
    with np.errstate(invalid="ignore"):
        up = (de > 0) & (dh > 0)
        dn = (de < 0) & (dh < 0)
    return np.where(up, 1, np.where(dn, -1, 0)).astype(np.int8)


def thermometer(high, low) -> np.ndarray:
    """Рыночный термометр: насколько бар вышел за диапазон предыдущего — max(H − H[1], L[1] − L), не меньше 0."""
    h, lo = core.f64(high), core.f64(low)
    with np.errstate(invalid="ignore"):
        return np.fmax(np.fmax(h - core.shift(h, 1), core.shift(lo, 1) - lo), 0.0)


def safezone_stops(high, low, lookback: int = 10, k: float = 2.5) -> tuple[np.ndarray, np.ndarray]:
    """Уровни стопов SafeZone на следующий бар (long, short) по данным до бара i включительно.

    Шум лонга — средний «прорыв вниз» L[1] − L по барам, где он был, за lookback баров;
    стоп лонга = L − k × шум. Шорт — зеркально по максимумам. Нет прорывов в окне — NaN.
    """
    h, lo = core.f64(high), core.f64(low)
    pen_dn = core.shift(lo, 1) - lo
    pen_up = h - core.shift(h, 1)
    with np.errstate(invalid="ignore", divide="ignore"):
        n_dn = core.rolling_sum((pen_dn > 0).astype(np.float64), lookback)
        n_up = core.rolling_sum((pen_up > 0).astype(np.float64), lookback)
        avg_dn = core.rolling_sum(np.fmax(pen_dn, 0.0), lookback) / n_dn
        avg_up = core.rolling_sum(np.fmax(pen_up, 0.0), lookback) / n_up
        stop_long = np.where(n_dn > 0, lo - k * avg_dn, np.nan)
        stop_short = np.where(n_up > 0, h + k * avg_up, np.nan)
    return stop_long, stop_short
