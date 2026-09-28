"""feature_rules: стопы из книг (SafeZone, за бар, дневной ATR) и частичная фиксация (Дуглас)."""

import numpy as np
import polars as pl
import pytest

from quant.research.features import FEATURES
from quant.strategies import get_strategy
from quant.strategies.feature_rules import stop_distance

RULES = {"rules_long": [["rsi14", "<=", 35]], "rules_short": [["rsi14", ">=", 65]], "rearm_bars": 6}


def _frame(bars_1h, bars_1m, meta, **params):
    return get_strategy("feature_rules", {**RULES, **params}).compute(bars_1h, hh_symbol="SYNUSDT", tf="1h",
                                                                       bars_1m=bars_1m, meta=meta)


def test_book_features_are_rule_features():
    for name in ("fi2n", "impulse", "htf_ema26_slope_pct", "d1_ema50_dist_pct", "sweep_low48", "sz_long_atr"):
        assert name in FEATURES


@pytest.mark.parametrize("mode", ["atr", "safezone", "bar"])
def test_stop_modes(bars_1h, bars_1m, meta, mode):
    fr = _frame(bars_1h, bars_1m, meta, sl_mode=mode, sl_atr=1.5, tp_r=2.0)
    sig = fr.filter(pl.col("signal_long"))
    assert sig.height > 0
    risk = (sig["close"] - sig["sl_long"]).to_numpy()
    atr = sig["atr"].to_numpy()
    assert np.all(risk > 0)
    if mode == "atr":
        np.testing.assert_allclose(risk, 1.5 * atr, rtol=1e-9)
    else:                                              # ограничено [0.5, 3] ATR
        assert np.all(risk >= 0.5 * atr - 1e-9) and np.all(risk <= 3.0 * atr + 1e-9)
    np.testing.assert_allclose((sig["tp1_long"] - sig["close"]).to_numpy(), 2.0 * risk, rtol=1e-9)


def test_stop_distance_d1_atr():
    f = {"_close": np.array([100.0, 200.0]), "_atr": np.array([1.0, 2.0]), "d1_atr_pct": np.array([3.0, np.nan])}
    d = stop_distance({"sl_mode": "d1_atr", "sl_atr": 2.0}, f, "long")
    assert d[0] == pytest.approx(6.0)                  # 2 × 3% × 100
    assert np.isnan(d[1])


def test_partial_exit_levels(bars_1h, bars_1m, meta):
    strat = get_strategy("feature_rules", {**RULES, "tp_r": 1.0, "tp2_r": 3.0, "tp1_frac": 0.5})
    fr = strat.compute(bars_1h, hh_symbol="SYNUSDT", tf="1h", bars_1m=bars_1m, meta=meta)
    sig = fr.filter(pl.col("signal_short"))
    assert sig.height > 0
    risk = (sig["sl_short"] - sig["close"]).to_numpy()
    np.testing.assert_allclose((sig["close"] - sig["tp2_short"]).to_numpy(), 3.0 * risk, rtol=1e-9)
    s = strat.signals_from(fr, hh_symbol="SYNUSDT", tf="1h")
    assert (s["tp1_frac"] == 0.5).all() and s["tp2"].is_not_null().all()


def test_bad_sl_mode():
    with pytest.raises(ValueError):
        get_strategy("feature_rules", {**RULES, "sl_mode": "chandelier"})
