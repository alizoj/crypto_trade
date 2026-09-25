"""Объёмные индикаторы: VWAP (якорный), AVWAP, RVOL, CVD, Volume Profile (раздел 6 ТЗ).

VWAP — по барам (hlc3 × volume), а не по сделкам. Якорь сессии:
- utc_day  — сутки с 00:00 UTC (сессия TradingView для BINANCE:*.P, для паритета);
- prop_day — «проп-день» с границы day_boundary_utc (для Crypto+TradFi 20:00 UTC).
Новый период начинается с бара, чьё время открытия попало в новые сутки.
CVD — из taker_buy_volume свечей Binance: delta = 2 * taker_buy_volume - volume (раздел 3).
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numba import njit

from quant.indicators.core import f64, sma

MS_PER_DAY = 86_400_000
PROP_DAY_BOUNDARY_MIN = 20 * 60   # дефолт; фактическое значение — challenge.yaml markets.*.day_boundary_utc


def session_key(ts_ms, anchor: str = "utc_day", boundary_min: int = PROP_DAY_BOUNDARY_MIN) -> np.ndarray:
    """Ключ периода VWAP по времени открытия бара (мс UTC)."""
    ts = np.asarray(ts_ms, dtype=np.int64)
    if anchor == "utc_day":
        return ts // MS_PER_DAY
    if anchor == "prop_day":
        return (ts + ((1440 - boundary_min) % 1440) * 60_000) // MS_PER_DAY
    raise ValueError(f"vwap anchor: utc_day | prop_day, получено {anchor!r}")


@njit(cache=True)
def _vwap_nb(src, vol, key):
    n = len(src)
    out = np.full(n, np.nan)
    spv = 0.0
    sv = 0.0
    started = False
    for i in range(n):
        if not started or key[i] != key[i - 1]:
            spv = 0.0
            sv = 0.0
            started = True
        s = src[i]
        v = vol[i]
        if not (np.isnan(s) or np.isnan(v)):
            spv = s * v + spv
            sv = v + sv
        if sv > 0:
            out[i] = spv / sv
    return out


@njit(cache=True)
def _avwap_dyn_nb(src, vol, anchors):
    n = len(src)
    out = np.full(n, np.nan)
    cur = -1
    spv = 0.0
    sv = 0.0
    for i in range(n):
        a = anchors[i]
        if a < 0 or a > i:
            cur = -1
            continue
        if a != cur:
            cur = a
            spv = 0.0
            sv = 0.0
            for j in range(a, i + 1):
                if not (np.isnan(src[j]) or np.isnan(vol[j])):
                    spv = src[j] * vol[j] + spv
                    sv = vol[j] + sv
        else:
            if not (np.isnan(src[i]) or np.isnan(vol[i])):
                spv = src[i] * vol[i] + spv
                sv = vol[i] + sv
        if sv > 0:
            out[i] = spv / sv
    return out


@njit(cache=True)
def _cum_reset_nb(x, key, use_key):
    n = len(x)
    out = np.empty(n)
    s = 0.0
    for i in range(n):
        if use_key and i > 0 and key[i] != key[i - 1]:
            s = 0.0
        if not np.isnan(x[i]):
            s += x[i]
        out[i] = s
    return out


@njit(cache=True)
def _profile_nb(high, low, vol, edges):
    nb = len(edges) - 1
    out = np.zeros(nb)
    lo0 = edges[0]
    step = (edges[nb] - lo0) / nb
    for i in range(len(high)):
        h = high[i]
        lo = low[i]
        v = vol[i]
        if np.isnan(h) or np.isnan(lo) or np.isnan(v) or v <= 0:
            continue
        if h <= lo:
            b = int((lo - lo0) / step)
            if b >= nb:
                b = nb - 1
            if b < 0:
                b = 0
            out[b] += v
            continue
        b0 = int((lo - lo0) / step)
        b1 = int((h - lo0) / step)
        if b0 < 0:
            b0 = 0
        if b1 >= nb:
            b1 = nb - 1
        rng = h - lo
        for b in range(b0, b1 + 1):
            a = max(lo, edges[b])
            z = min(h, edges[b + 1])
            if z > a:
                out[b] += v * (z - a) / rng
    return out


def vwap(src, volume, key) -> np.ndarray:
    """ta.vwap(src) с якорем: суммы src*volume и volume сбрасываются при смене key.
    Нулевой накопленный объём → NaN (в Pine 0/0 = na)."""
    return _vwap_nb(f64(src), f64(volume), np.ascontiguousarray(key, dtype=np.int64))


def avwap(src, volume, anchor_idx: int) -> np.ndarray:
    """AVWAP от бара anchor_idx (включительно); до якоря — NaN."""
    n = len(src)
    anchors = np.full(n, -1, dtype=np.int64)
    if 0 <= anchor_idx < n:
        anchors[anchor_idx:] = anchor_idx
    return _avwap_dyn_nb(f64(src), f64(volume), anchors)


def avwap_dynamic(src, volume, anchors) -> np.ndarray:
    """AVWAP с переменным якорем: anchors[i] — индекс бара-якоря, известного на баре i (-1 — нет).
    Значение на баре i = Σ src*vol / Σ vol по барам anchors[i]..i — зависит только от прошлого."""
    return _avwap_dyn_nb(f64(src), f64(volume), np.ascontiguousarray(anchors, dtype=np.int64))


def rvol(volume, n: int = 20) -> np.ndarray:
    """RVOL = volume / sma(volume, n) (текущий бар входит в среднее); среднее 0 → NaN."""
    v = f64(volume)
    m = sma(v, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        return np.where(m > 0, v / m, np.nan)


def cvd_delta(volume, taker_buy_volume) -> np.ndarray:
    """Дельта объёма бара: 2 * taker_buy_volume - volume."""
    return 2.0 * f64(taker_buy_volume) - f64(volume)


def cvd(volume, taker_buy_volume, key=None) -> np.ndarray:
    """Кумулятивная дельта; при заданном key — сброс при смене периода (якорный CVD).
    NaN-дельта (нет taker_buy_volume) вклада не даёт."""
    d = cvd_delta(volume, taker_buy_volume)
    use_key = key is not None
    k = np.ascontiguousarray(key if use_key else np.zeros(len(d)), dtype=np.int64)
    return _cum_reset_nb(d, k, use_key)


@dataclass(frozen=True)
class VolumeProfile:
    edges: np.ndarray      # границы корзин, n_bins + 1
    volume: np.ndarray     # объём по корзинам
    poc: float             # центр корзины с максимальным объёмом
    vah: float             # верхняя граница value area
    val: float             # нижняя граница value area
    va_volume: float       # объём внутри value area

    @property
    def centers(self) -> np.ndarray:
        return (self.edges[:-1] + self.edges[1:]) / 2.0

    @property
    def total(self) -> float:
        return float(self.volume.sum())


def volume_profile(high, low, volume, n_bins: int = 50, value_area: float = 0.70) -> VolumeProfile | None:
    """Профиль объёма окна баров. Объём бара распределяется равномерно по его диапазону [low, high].

    Value area — классически: от POC расширяемся на соседнюю корзину с большим объёмом
    (при равенстве — вверх), пока не наберём value_area от общего объёма.
    """
    h, lo, v = f64(high), f64(low), f64(volume)
    ok = ~(np.isnan(h) | np.isnan(lo) | np.isnan(v))
    if not ok.any() or v[ok].sum() <= 0:
        return None
    pmin, pmax = float(lo[ok].min()), float(h[ok].max())
    if pmax <= pmin:
        pmax = pmin + max(abs(pmin) * 1e-9, 1e-12)
    edges = np.linspace(pmin, pmax, n_bins + 1)
    vols = _profile_nb(h, lo, v, edges)
    poc_i = int(np.argmax(vols))
    total = float(vols.sum())
    lo_i = hi_i = poc_i
    acc = float(vols[poc_i])
    while acc < value_area * total - 1e-12 * total and (lo_i > 0 or hi_i < n_bins - 1):
        up = vols[hi_i + 1] if hi_i + 1 < n_bins else -1.0
        dn = vols[lo_i - 1] if lo_i - 1 >= 0 else -1.0
        if up >= dn:
            hi_i += 1
            acc += up
        else:
            lo_i -= 1
            acc += dn
    centers = (edges[:-1] + edges[1:]) / 2.0
    return VolumeProfile(edges=edges, volume=vols, poc=float(centers[poc_i]), vah=float(edges[hi_i + 1]),
                         val=float(edges[lo_i]), va_volume=acc)
