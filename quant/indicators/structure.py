"""Рыночная структура по подтверждённым свингам: HH/HL, BOS/CHoCH, liquidity sweep (раздел 6 ТЗ).

Свинги — ta.pivothigh(high)/ta.pivotlow(low) с left/right; уровень свинга становится известен
на баре подтверждения (p + right), раньше он в расчётах не участвует.

На каждом баре i (после учёта подтверждённых на нём свингов):
- слом вверх: close > активного уровня последнего swing high. Если последний слом был вниз
  (trend = -1) — CHoCH, иначе BOS; trend = +1, уровень «израсходован»;
- sweep вверху (медвежий): high > уровня, но close <= уровня — снятие ликвидности над максимумом;
  фиксируется один раз на уровень;
- зеркально для swing low (слом вниз, бычий sweep под минимумом).
Новый подтверждённый свинг заменяет уровень. Состояние зависит только от последних свингов
и последнего слома — конечная память, поэтому расчёт на окне с прогревом совпадает с полным.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numba import njit

from quant.indicators.core import f64
from quant.indicators.pivots import pivothigh, pivotlow


@dataclass(frozen=True)
class Structure:
    bos_up: np.ndarray
    bos_dn: np.ndarray
    choch_up: np.ndarray
    choch_dn: np.ndarray
    sweep_up: np.ndarray        # бычий sweep: прокол swing low с закрытием выше уровня
    sweep_dn: np.ndarray        # медвежий sweep: прокол swing high с закрытием ниже уровня
    trend: np.ndarray           # int8: направление последнего слома (+1/-1/0)
    hh_hl: np.ndarray           # int8: +1 — HH и HL, -1 — LH и LL, 0 — смешанная/нет данных
    swing_high: np.ndarray      # уровень последнего подтверждённого swing high
    swing_low: np.ndarray
    swing_high_idx: np.ndarray  # индекс бара этого свинга (-1 — нет)
    swing_low_idx: np.ndarray


@njit(cache=True)
def _structure_nb(high, low, close, ph_idx, pl_idx):
    n = len(close)
    bos_up = np.zeros(n, dtype=np.bool_)
    bos_dn = np.zeros(n, dtype=np.bool_)
    choch_up = np.zeros(n, dtype=np.bool_)
    choch_dn = np.zeros(n, dtype=np.bool_)
    sweep_up = np.zeros(n, dtype=np.bool_)
    sweep_dn = np.zeros(n, dtype=np.bool_)
    trend_o = np.zeros(n, dtype=np.int8)
    hhhl = np.zeros(n, dtype=np.int8)
    sh_o = np.full(n, np.nan)
    sl_o = np.full(n, np.nan)
    shi_o = np.full(n, -1, dtype=np.int64)
    sli_o = np.full(n, -1, dtype=np.int64)

    sh = np.nan
    sh_prev = np.nan
    sh_i = -1
    sh_active = False
    sh_swept = False
    sl = np.nan
    sl_prev = np.nan
    sl_i = -1
    sl_active = False
    sl_swept = False
    trend = 0
    for i in range(n):
        p = ph_idx[i]
        if p >= 0:
            sh_prev = sh
            sh = high[p]
            sh_i = p
            sh_active = True
            sh_swept = False
        p = pl_idx[i]
        if p >= 0:
            sl_prev = sl
            sl = low[p]
            sl_i = p
            sl_active = True
            sl_swept = False
        c = close[i]
        if sh_active and not np.isnan(c):
            if c > sh:
                if trend == -1:
                    choch_up[i] = True
                else:
                    bos_up[i] = True
                trend = 1
                sh_active = False
            elif (not sh_swept) and high[i] > sh:
                sweep_dn[i] = True
                sh_swept = True
        if sl_active and not np.isnan(c):
            if c < sl:
                if trend == 1:
                    choch_dn[i] = True
                else:
                    bos_dn[i] = True
                trend = -1
                sl_active = False
            elif (not sl_swept) and low[i] < sl:
                sweep_up[i] = True
                sl_swept = True
        trend_o[i] = trend
        if not (np.isnan(sh_prev) or np.isnan(sl_prev)):
            if sh > sh_prev and sl > sl_prev:
                hhhl[i] = 1
            elif sh < sh_prev and sl < sl_prev:
                hhhl[i] = -1
        sh_o[i] = sh
        sl_o[i] = sl
        shi_o[i] = sh_i
        sli_o[i] = sl_i
    return (bos_up, bos_dn, choch_up, choch_dn, sweep_up, sweep_dn, trend_o, hhhl,
            sh_o, sl_o, shi_o, sli_o)


def market_structure(high, low, close, left: int = 5, right: int = 2) -> Structure:
    """Структура рынка по подтверждённым свингам (см. docstring модуля)."""
    h, lo, c = f64(high), f64(low), f64(close)
    _, ph_idx = pivothigh(h, left, right)
    _, pl_idx = pivotlow(lo, left, right)
    r = _structure_nb(h, lo, c, ph_idx, pl_idx)
    return Structure(*r)
