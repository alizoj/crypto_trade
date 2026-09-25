"""Пивоты и дивергенции — только подтверждённые (раздел 6 ТЗ).

ta.pivothigh(src, left, right): пивот на баре p известен только на баре i = p + right
(подтверждение). Функции возвращают значение пивота на баре подтверждения (NaN на остальных)
и индекс бара пивота (-1 на остальных) — время сигнала = время подтверждения.

Правило равенств (# ASSUMPTION, проверяется golden-тестом по колонкам ph/pl): слева строго
(src[p] > src[p-k]), справа нестрого (src[p] >= src[p+k]) — из «плато» равных вершин пивотом
становится левая. strict_right=True делает правую сторону тоже строгой.
Окно с NaN пивота не даёт.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numba import njit

from quant.indicators.core import f64


@njit(cache=True)
def _pivot_nb(src, left, right, is_high, strict_right):
    n = len(src)
    vals = np.full(n, np.nan)
    idx = np.full(n, -1, dtype=np.int64)
    for i in range(left + right, n):
        p = i - right
        v = src[p]
        if np.isnan(v):
            continue
        ok = True
        for k in range(1, left + 1):
            w = src[p - k]
            if np.isnan(w) or (w >= v if is_high else w <= v):
                ok = False
                break
        if ok:
            for k in range(1, right + 1):
                w = src[p + k]
                if np.isnan(w):
                    ok = False
                    break
                if is_high:
                    bad = w >= v if strict_right else w > v
                else:
                    bad = w <= v if strict_right else w < v
                if bad:
                    ok = False
                    break
        if ok:
            vals[i] = v
            idx[i] = p
    return vals, idx


def pivothigh(src, left: int, right: int, strict_right: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """ta.pivothigh(src, left, right) → (значение на баре подтверждения, индекс бара пивота)."""
    return _pivot_nb(f64(src), int(left), int(right), True, bool(strict_right))


def pivotlow(src, left: int, right: int, strict_right: bool = False) -> tuple[np.ndarray, np.ndarray]:
    """ta.pivotlow(src, left, right) → (значение на баре подтверждения, индекс бара пивота)."""
    return _pivot_nb(f64(src), int(left), int(right), False, bool(strict_right))


@dataclass(frozen=True)
class Divergences:
    bull: np.ndarray          # регулярная бычья: цена LL, осциллятор HL
    hidden_bull: np.ndarray   # скрытая бычья: цена HL, осциллятор LL
    bear: np.ndarray          # регулярная медвежья: цена HH, осциллятор LH
    hidden_bear: np.ndarray   # скрытая медвежья: цена LH, осциллятор HH
    low_pivot_idx: np.ndarray   # индекс пивота-минимума осциллятора (на баре подтверждения), -1
    high_pivot_idx: np.ndarray


@njit(cache=True)
def _div_nb(osc, price, pidx, rlo, rhi, lower_is_bull):
    # lower_is_bull=True — пивоты-минимумы (бычьи), иначе максимумы (медвежьи).
    n = len(osc)
    reg = np.zeros(n, dtype=np.bool_)
    hid = np.zeros(n, dtype=np.bool_)
    prev_i = -1
    prev_p = -1
    for i in range(n):
        p = pidx[i]
        if p < 0:
            continue
        if prev_i >= 0:
            bars = i - prev_i - 1          # ta.barssince(found[1]) в индикаторе TradingView
            if rlo <= bars <= rhi:
                o, o0 = osc[p], osc[prev_p]
                x, x0 = price[p], price[prev_p]
                if lower_is_bull:
                    if x < x0 and o > o0:
                        reg[i] = True
                    if x > x0 and o < o0:
                        hid[i] = True
                else:
                    if x > x0 and o < o0:
                        reg[i] = True
                    if x < x0 and o > o0:
                        hid[i] = True
        prev_i = i
        prev_p = p
    return reg, hid


def divergences(osc, low, high, left: int = 5, right: int = 5, range_lower: int = 5,
                range_upper: int = 60) -> Divergences:
    """Дивергенции цены и осциллятора по подтверждённым пивотам осциллятора — как во встроенном
    индикаторе TradingView «RSI Divergence Indicator»: пивоты ищутся на osc (left/right),
    цена берётся на тех же барах (low для минимумов, high для максимумов), сравнение с предыдущим
    пивотом того же типа, расстояние ta.barssince(found[1]) ∈ [range_lower, range_upper].
    Флаг ставится на баре подтверждения (p + right)."""
    o = f64(osc)
    _, pl_idx = pivotlow(o, left, right)
    _, ph_idx = pivothigh(o, left, right)
    bull, hbull = _div_nb(o, f64(low), pl_idx, int(range_lower), int(range_upper), True)
    bear, hbear = _div_nb(o, f64(high), ph_idx, int(range_lower), int(range_upper), False)
    return Divergences(bull=bull, hidden_bull=hbull, bear=bear, hidden_bear=hbear,
                       low_pivot_idx=pl_idx, high_pivot_idx=ph_idx)
