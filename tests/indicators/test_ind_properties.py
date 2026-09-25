"""Свойства индикаторов: диапазоны, границы VWAP, подтверждение пивотов, профиль объёма, look-ahead."""

from __future__ import annotations

import numpy as np
import pytest

from quant.indicators import (
    atr, bb, dmi, ema, market_structure, pivothigh, pivotlow, rma, rsi, session_key, sma, tr, volume_profile,
    vwap,
)
from quant.indicators.core import hlc3


def test_rsi_range(ohlcv):
    r = rsi(ohlcv["close"], 14)
    ok = ~np.isnan(r)
    assert ok.sum() == len(r) - 14
    assert (r[ok] >= 0).all() and (r[ok] <= 100).all()


def test_atr_tr_nonnegative(ohlcv):
    h, lo, c = ohlcv["high"], ohlcv["low"], ohlcv["close"]
    t = tr(h, lo, c)
    assert (t >= h - lo - 1e-12).all() and t[0] == h[0] - lo[0]
    a = atr(h, lo, c, 14)
    assert np.isnan(a[:13]).all() and (a[13:] >= 0).all()
    assert a[13] == pytest.approx(t[:14].mean(), rel=1e-12)


def test_dmi_ranges(ohlcv):
    p, m, a = dmi(ohlcv["high"], ohlcv["low"], ohlcv["close"], 14, 14)
    for x in (p, m, a):
        ok = ~np.isnan(x)
        assert (x[ok] >= 0).all() and (x[ok] <= 100).all()


def test_bb_order(ohlcv):
    basis, up, lo = bb(ohlcv["close"], 20, 2.0)
    ok = ~np.isnan(basis)
    assert (lo[ok] <= basis[ok]).all() and (basis[ok] <= up[ok]).all()


def test_ema_seed_variants(ohlcv):
    x = ohlcv["close"]
    e = ema(x, 20)
    assert np.isnan(e[:19]).all()
    assert e[19] == pytest.approx(x[:20].mean(), rel=1e-12)          # встроенный ta.ema: SMA-seed
    f = ema(x, 20, seed="first")
    assert f[0] == x[0]                                               # pine_ema из справочника
    with pytest.raises(ValueError):
        ema(x, 20, seed="zero")


@pytest.mark.parametrize("anchor", ["utc_day", "prop_day"])
def test_vwap_inside_session_range(ohlcv, anchor):
    h, lo, c, v = ohlcv["high"], ohlcv["low"], ohlcv["close"], ohlcv["volume"]
    key = session_key(ohlcv["ts"], anchor)
    w = vwap(hlc3(h, lo, c), v, key)
    start = 0
    for i in range(len(c)):
        if i and key[i] != key[i - 1]:
            start = i
            assert w[i] == pytest.approx(hlc3(h[i:i + 1], lo[i:i + 1], c[i:i + 1])[0], rel=1e-12)  # сброс
        assert lo[start:i + 1].min() - 1e-9 <= w[i] <= h[start:i + 1].max() + 1e-9


def test_vwap_zero_volume():
    src = np.array([10.0, 11.0, 12.0, 13.0])
    v = np.array([0.0, 2.0, 0.0, 2.0])
    w = vwap(src, v, np.zeros(4, dtype=np.int64))
    assert np.isnan(w[0]) and w[1] == 11.0 and w[2] == 11.0 and w[3] == 12.0


@pytest.mark.parametrize("left,right", [(5, 2), (3, 3)])
def test_pivot_confirmed_exactly_after_right(ohlcv, left, right):
    for fn, src in ((pivothigh, ohlcv["high"]), (pivotlow, ohlcv["low"])):
        vals, idx = fn(src, left, right)
        hits = np.flatnonzero(idx >= 0)
        assert len(hits) > 10
        assert (idx[hits] == hits - right).all()
        assert (vals[hits] == src[idx[hits]]).all()
        for i in hits[:20]:
            # на данных до подтверждения (без бара i) пивота на баре p нет нигде
            _, idx_before = fn(src[:i], left, right)
            assert not (idx_before == i - right).any()
            # на данных ровно до бара подтверждения — есть, на том же баре
            _, idx_at = fn(src[:i + 1], left, right)
            assert idx_at[i] == i - right


def test_structure_prefix_invariance(ohlcv):
    """Структура на усечённых данных совпадает с префиксом полного расчёта (нет look-ahead)."""
    h, lo, c = ohlcv["high"], ohlcv["low"], ohlcv["close"]
    full = market_structure(h, lo, c, 5, 2)
    for cut in (300, 777, 1200):
        part = market_structure(h[:cut], lo[:cut], c[:cut], 5, 2)
        for name in ("bos_up", "bos_dn", "choch_up", "choch_dn", "sweep_up", "sweep_dn", "trend", "hh_hl",
                     "swing_high_idx", "swing_low_idx"):
            assert (getattr(part, name) == getattr(full, name)[:cut]).all(), name
    assert full.bos_up.sum() + full.choch_up.sum() > 0 and full.sweep_up.sum() > 0
    # CHoCH вверх — только после медвежьего слома
    for i in np.flatnonzero(full.choch_up):
        assert full.trend[i - 1] == -1


def test_structure_levels_known_only_after_confirmation(ohlcv):
    h, lo, c = ohlcv["high"], ohlcv["low"], ohlcv["close"]
    st = market_structure(h, lo, c, 5, 2)
    idx = st.swing_high_idx
    ok = idx >= 0
    assert (np.arange(len(h))[ok] - idx[ok] >= 2).all()     # уровень известен не раньше p + right


def test_volume_profile(ohlcv):
    h, lo, v = ohlcv["high"][-300:], ohlcv["low"][-300:], ohlcv["volume"][-300:]
    vp = volume_profile(h, lo, v, n_bins=40)
    assert vp is not None
    assert vp.total == pytest.approx(v.sum(), rel=1e-9)
    assert vp.val <= vp.poc <= vp.vah
    assert vp.va_volume >= 0.70 * vp.total
    assert lo.min() == pytest.approx(vp.edges[0]) and h.max() == pytest.approx(vp.edges[-1])
    assert vp.volume.argmax() == int(np.argmin(np.abs(vp.centers - vp.poc)))
    assert volume_profile(h, lo, np.zeros_like(v)) is None


def test_volume_profile_point_bars():
    # бары с high == low: весь объём — в корзину цены
    h = np.array([10.0, 10.0, 12.0])
    vp = volume_profile(h, h.copy(), np.array([1.0, 1.0, 5.0]), n_bins=4)
    assert vp.volume[0] == 2.0 and vp.volume[-1] == 5.0 and vp.poc > 11.0


def test_prefix_invariance_all(ohlcv):
    """Любой индикатор на префиксе данных = префикс полного расчёта (рекурсии не смотрят вперёд)."""
    h, lo, c = ohlcv["high"], ohlcv["low"], ohlcv["close"]
    cut = 900
    pairs = [
        (sma(c, 20), sma(c[:cut], 20)), (ema(c, 50), ema(c[:cut], 50)), (rma(c, 14), rma(c[:cut], 14)),
        (rsi(c, 14), rsi(c[:cut], 14)), (atr(h, lo, c, 14), atr(h[:cut], lo[:cut], c[:cut], 14)),
        (dmi(h, lo, c)[2], dmi(h[:cut], lo[:cut], c[:cut])[2]),
    ]
    for full, part in pairs:
        np.testing.assert_array_equal(full[:cut], part)
