"""Сравнение с медленными эталонами — построчные переводы кода Pine (справочник v6) на чистый Python.

Эталоны намеренно «тупые»: история через индексы x[i-k], без numpy-векторизации и numba.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from quant.indicators import (
    atr, bb, cvd, divergences, dmi, ema, obv, pivothigh, pivotlow, rma, rsi, session_key, sma, stdev,
    tr, vwap, wma,
)
from quant.indicators.core import barssince, change, fixnan, highest, lowest, rolling_sum, valuewhen

NAN = float("nan")
RTOL = 1e-12


def isna(v) -> bool:
    return v is None or (isinstance(v, float) and math.isnan(v))


# ------------------------------------------------------------------ эталоны


def ref_sma_at(x, n, i):
    if i < n - 1:
        return NAN
    w = x[i - n + 1:i + 1]
    if any(isna(v) for v in w):
        return NAN
    return sum(w) / n


def ref_sma(x, n):
    return [ref_sma_at(x, n, i) for i in range(len(x))]


def ref_rec(src, n, alpha):
    # sum := na(sum[1]) ? ta.sma(src, length) : alpha * src + (1 - alpha) * nz(sum[1])
    out, prev = [], NAN
    for i in range(len(src)):
        prev = ref_sma_at(src, n, i) if isna(prev) else alpha * src[i] + (1 - alpha) * prev
        out.append(prev)
    return out


def ref_rma(src, n):
    return ref_rec(src, n, 1.0 / n)


def ref_ema_builtin(src, n):
    return ref_rec(src, n, 2.0 / (n + 1))


def ref_pine_ema(src, n):
    # pine_ema из справочника: sum := na(sum[1]) ? src : alpha * src + (1 - alpha) * nz(sum[1])
    alpha, out, prev = 2.0 / (n + 1), [], NAN
    for v in src:
        prev = v if isna(prev) else alpha * v + (1 - alpha) * prev
        out.append(prev)
    return out


def ref_wma(x, n):
    out = []
    for i in range(len(x)):
        if i < n - 1:
            out.append(NAN)
            continue
        norm = s = 0.0
        for k in range(n):
            weight = (n - k) * n
            norm += weight
            s += x[i - k] * weight
        out.append(s / norm)
    return out


def ref_stdev(x, n):
    out = []
    for i in range(len(x)):
        avg = ref_sma_at(x, n, i)
        if isna(avg):
            out.append(NAN)
            continue
        ss = 0.0
        for k in range(n):
            d = x[i - k] + (-avg)
            if abs(d) <= 1e-10:
                d = 0.0
            ss += d * d
        out.append(math.sqrt(ss / n))
    return out


def ref_tr(h, lo, c, handle_na):
    out = []
    for i in range(len(c)):
        if i == 0:
            out.append(h[i] - lo[i] if handle_na else NAN)
        else:
            out.append(max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1])))
    return out


def ref_rsi(x, n):
    u = [NAN] + [max(x[i] - x[i - 1], 0.0) for i in range(1, len(x))]
    d = [NAN] + [max(x[i - 1] - x[i], 0.0) for i in range(1, len(x))]
    up, dn = ref_rma(u, n), ref_rma(d, n)
    out = []
    for a, b in zip(up, dn):
        if isna(a) or isna(b):
            out.append(NAN)
        else:
            out.append(100.0 if b == 0 else (0.0 if a == 0 else 100.0 - 100.0 / (1.0 + a / b)))
    return out


def ref_dmi(h, lo, c, n, adx_n):
    m = len(c)
    up = [NAN] + [h[i] - h[i - 1] for i in range(1, m)]
    down = [NAN] + [-(lo[i] - lo[i - 1]) for i in range(1, m)]
    plus_dm = [NAN if isna(u) else (u if (u > dd and u > 0) else 0.0) for u, dd in zip(up, down)]
    minus_dm = [NAN if isna(dd) else (dd if (dd > u and dd > 0) else 0.0) for u, dd in zip(up, down)]
    trur = ref_rma(ref_tr(h, lo, c, False), n)
    rp, rm = ref_rma(plus_dm, n), ref_rma(minus_dm, n)
    plus, minus = [], []
    lp = lm = NAN
    for i in range(m):
        vp = NAN if isna(rp[i]) or isna(trur[i]) else 100 * rp[i] / trur[i]
        vm = NAN if isna(rm[i]) or isna(trur[i]) else 100 * rm[i] / trur[i]
        lp = lp if isna(vp) else vp          # fixnan
        lm = lm if isna(vm) else vm
        plus.append(lp)
        minus.append(lm)
    dx = []
    for a, b in zip(plus, minus):
        if isna(a) or isna(b):
            dx.append(NAN)
        else:
            s = a + b
            dx.append(abs(a - b) / (1 if s == 0 else s))
    adx = [NAN if isna(v) else 100 * v for v in ref_rma(dx, adx_n)]
    return plus, minus, adx


def ref_vwap(src, v, key):
    out, spv, sv = [], 0.0, 0.0
    for i in range(len(src)):
        if i == 0 or key[i] != key[i - 1]:
            spv, sv = src[i] * v[i], v[i]
        else:
            spv, sv = src[i] * v[i] + spv, v[i] + sv
        out.append(spv / sv if sv != 0 else NAN)
    return out


def ref_pivot(src, left, right, high):
    vals, idx = [NAN] * len(src), [-1] * len(src)
    for i in range(left + right, len(src)):
        p = i - right
        v = src[p]
        lft = [src[p - k] for k in range(1, left + 1)]
        rgt = [src[p + k] for k in range(1, right + 1)]
        ok = all(w < v for w in lft) and all(w <= v for w in rgt) if high else \
            all(w > v for w in lft) and all(w >= v for w in rgt)
        if ok:
            vals[i], idx[i] = v, p
    return vals, idx


def ref_barssince(cond, i):
    for k in range(i, -1, -1):
        if cond[k]:
            return i - k
    return None


def ref_divergences(osc, low, high, lbl, lbr, rlo, rhi):
    """Перевод индикатора TradingView «RSI Divergence Indicator» (valuewhen/barssince)."""
    m = len(osc)
    pl_found = [not isna(v) for v in ref_pivot(osc, lbl, lbr, False)[0]]
    ph_found = [not isna(v) for v in ref_pivot(osc, lbl, lbr, True)[0]]
    res = {k: [False] * m for k in ("bull", "hidden_bull", "bear", "hidden_bear")}
    for found, price, reg_key, hid_key, lows in ((pl_found, low, "bull", "hidden_bull", True),
                                                (ph_found, high, "bear", "hidden_bear", False)):
        shifted = [False] + found[:-1]            # found[1]
        for i in range(m):
            if not found[i]:
                continue
            prev = [j for j in range(i) if found[j]]
            if not prev:
                continue
            j = prev[-1]                           # valuewhen(found, x[lbR], 1)
            bars = ref_barssince(shifted, i)
            in_range = bars is not None and rlo <= bars <= rhi
            o, o0 = osc[i - lbr], osc[j - lbr]
            x, x0 = price[i - lbr], price[j - lbr]
            if lows:
                res[reg_key][i] = (x < x0) and (o > o0 and in_range)
                res[hid_key][i] = (x > x0) and (o < o0 and in_range)
            else:
                res[reg_key][i] = (x > x0) and (o < o0 and in_range)
                res[hid_key][i] = (x < x0) and (o > o0 and in_range)
    return res


def close(a, b):
    np.testing.assert_allclose(np.asarray(a, dtype=float), np.asarray(b, dtype=float), rtol=RTOL, atol=0,
                               equal_nan=True)


# ------------------------------------------------------------------ тесты


@pytest.mark.parametrize("n", [1, 5, 20, 200])
def test_sma_wma_stdev(ohlcv, n):
    x = list(ohlcv["close"])
    close(sma(ohlcv["close"], n), ref_sma(x, n))
    close(wma(ohlcv["close"], n), ref_wma(x, n))
    if n > 1:
        close(stdev(ohlcv["close"], n), ref_stdev(x, n))


@pytest.mark.parametrize("n", [2, 14, 50, 200])
def test_ema_rma(ohlcv, n):
    x = list(ohlcv["close"])
    close(ema(ohlcv["close"], n), ref_ema_builtin(x, n))
    close(ema(ohlcv["close"], n, seed="first"), ref_pine_ema(x, n))
    close(rma(ohlcv["close"], n), ref_rma(x, n))


def test_tr_atr(ohlcv):
    h, lo, c = (list(ohlcv[k]) for k in ("high", "low", "close"))
    close(tr(ohlcv["high"], ohlcv["low"], ohlcv["close"], True), ref_tr(h, lo, c, True))
    close(tr(ohlcv["high"], ohlcv["low"], ohlcv["close"], False), ref_tr(h, lo, c, False))
    close(atr(ohlcv["high"], ohlcv["low"], ohlcv["close"], 14), ref_rma(ref_tr(h, lo, c, True), 14))


@pytest.mark.parametrize("n", [2, 14, 21])
def test_rsi(ohlcv, n):
    close(rsi(ohlcv["close"], n), ref_rsi(list(ohlcv["close"]), n))


def test_rsi_zero_guards():
    up_only = np.arange(1.0, 40.0)
    assert np.all(rsi(up_only, 14)[14:] == 100.0)
    assert np.all(rsi(up_only[::-1].copy(), 14)[14:] == 0.0)
    flat = np.full(40, 5.0)
    assert np.all(rsi(flat, 14)[14:] == 100.0)          # rma(d)=0 → 100 (как встроенный RSI)
    assert np.isnan(rsi(flat, 14)[:14]).all()


@pytest.mark.parametrize("n,adx_n", [(14, 14), (10, 20)])
def test_dmi(ohlcv, n, adx_n):
    h, lo, c = (list(ohlcv[k]) for k in ("high", "low", "close"))
    p, m, a = dmi(ohlcv["high"], ohlcv["low"], ohlcv["close"], n, adx_n)
    rp, rm, ra = ref_dmi(h, lo, c, n, adx_n)
    close(p, rp)
    close(m, rm)
    close(a, ra)
    # прогрев: +DI с бара n (rma от ряда, начинающегося с na), ADX — с n + adx_n - 1
    assert np.isnan(p[:n]).all() and not np.isnan(p[n])
    assert np.isnan(a[:n + adx_n - 1]).all() and not np.isnan(a[n + adx_n - 1])


def test_bb(ohlcv):
    basis, up, lo = bb(ohlcv["close"], 20, 2.0)
    x = list(ohlcv["close"])
    sd = ref_stdev(x, 20)
    close(basis, ref_sma(x, 20))
    close(up, [b + 2.0 * s for b, s in zip(ref_sma(x, 20), sd)])
    close(lo, [b - 2.0 * s for b, s in zip(ref_sma(x, 20), sd)])


def test_obv(ohlcv):
    c, v = ohlcv["close"], ohlcv["volume"]
    ref, s = [], 0.0
    for i in range(len(c)):
        if i > 0:
            s += (1 if c[i] > c[i - 1] else -1 if c[i] < c[i - 1] else 0) * v[i]
        ref.append(s)
    close(obv(c, v), ref)


@pytest.mark.parametrize("anchor", ["utc_day", "prop_day"])
def test_vwap(ohlcv, anchor):
    h, lo, c, v = ohlcv["high"], ohlcv["low"], ohlcv["close"], ohlcv["volume"]
    key = session_key(ohlcv["ts"], anchor)
    src = (h + lo + c) / 3.0
    close(vwap(src, v, key), ref_vwap(list(src), list(v), list(key)))


def test_session_key_boundaries():
    day = 86_400_000
    # 00:00, 23:59, 00:00 след. суток, 19:59, 20:00 (UTC)
    t = np.array([0, day - 60_000, day, 20 * 3_600_000 - 60_000, 20 * 3_600_000], dtype=np.int64)
    assert list(session_key(t, "utc_day")) == [0, 0, 1, 0, 0]
    # проп-день: 20:00 UTC — уже новый день
    assert list(session_key(t, "prop_day")) == [0, 1, 1, 0, 1]
    assert list(session_key(t, "prop_day", boundary_min=0)) == [0, 0, 1, 0, 0]


def test_cvd(ohlcv):
    v, tb = ohlcv["volume"], ohlcv["taker_buy_volume"]
    close(cvd(v, tb), np.cumsum(2 * tb - v))
    key = session_key(ohlcv["ts"], "utc_day")
    anchored = cvd(v, tb, key)
    ref, s = [], 0.0
    for i in range(len(v)):
        if i > 0 and key[i] != key[i - 1]:
            s = 0.0
        s += 2 * tb[i] - v[i]
        ref.append(s)
    close(anchored, ref)


@pytest.mark.parametrize("left,right", [(5, 5), (5, 2), (3, 1), (2, 0)])
def test_pivots(ohlcv, left, right):
    for fn, src, high in ((pivothigh, ohlcv["high"], True), (pivotlow, ohlcv["low"], False)):
        vals, idx = fn(src, left, right)
        rv, ri = ref_pivot(list(src), left, right, high)
        close(vals, rv)
        assert list(idx) == ri


def test_pivot_ties():
    # «плато»: пивотом становится левая из равных вершин (слева строго, справа нестрого)
    x = np.array([1.0, 2.0, 5.0, 5.0, 2.0, 1.0, 0.5])
    vals, idx = pivothigh(x, 2, 2)
    assert list(np.flatnonzero(idx >= 0)) == [4] and idx[4] == 2
    vals, idx = pivothigh(x, 2, 2, strict_right=True)
    assert (idx < 0).all()


@pytest.mark.parametrize("lbl,lbr", [(5, 5), (5, 2)])
def test_divergences(ohlcv, lbl, lbr):
    osc = rsi(ohlcv["close"], 14)
    dv = divergences(osc, ohlcv["low"], ohlcv["high"], lbl, lbr, 5, 60)
    ref = ref_divergences(list(osc), list(ohlcv["low"]), list(ohlcv["high"]), lbl, lbr, 5, 60)
    for k in ("bull", "hidden_bull", "bear", "hidden_bear"):
        assert list(getattr(dv, k)) == ref[k], k
    assert dv.bull.sum() + dv.hidden_bull.sum() + dv.bear.sum() + dv.hidden_bear.sum() > 0


def test_helpers(ohlcv):
    x = ohlcv["close"]
    close(change(x, 3)[3:], x[3:] - x[:-3])
    close(highest(x, 10)[9:], [max(x[i - 9:i + 1]) for i in range(9, len(x))])
    close(lowest(x, 10)[9:], [min(x[i - 9:i + 1]) for i in range(9, len(x))])
    close(rolling_sum(x, 7)[6:], [sum(x[i - 6:i + 1]) for i in range(6, len(x))])
    y = np.array([NAN, 1.0, NAN, NAN, 3.0])
    close(fixnan(y), [NAN, 1.0, 1.0, 1.0, 3.0])
    cond = np.array([False, True, False, False, True, False])
    close(barssince(cond), [NAN, 0, 1, 2, 0, 1])
    close(valuewhen(cond, np.arange(6.0), 0), [NAN, 1, 1, 1, 4, 4])
    close(valuewhen(cond, np.arange(6.0), 1), [NAN, NAN, NAN, NAN, 1, 1])


def test_leading_nan_semantics():
    # производный ряд с ведущими na: отсчёт периода — от первого не-na значения
    x = np.concatenate([np.full(5, np.nan), np.arange(1.0, 31.0)])
    s = sma(x, 4)
    assert np.isnan(s[:8]).all() and s[8] == pytest.approx(2.5)
    r = rma(x, 4)
    assert np.isnan(r[:8]).all() and r[8] == pytest.approx(2.5)
    e = ema(x, 4, seed="first")
    assert np.isnan(e[:5]).all() and e[5] == 1.0
