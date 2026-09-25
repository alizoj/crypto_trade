from __future__ import annotations

import numpy as np
import polars as pl

from quant.calibration.model import (brier, ev_r, fit, fit_from_trades, live_check, load_active, reliability,
                                     reliability_ok, save_models, wilson)


def synthetic(n: int, seed: int = 0):
    rng = np.random.default_rng(seed)
    score = rng.uniform(0, 100, n)
    p_true = 0.3 + 0.4 * score / 100
    y = (rng.random(n) < p_true).astype(float)
    return score, y, p_true


def test_isotonic_is_calibrated_out_of_sample():
    s, y, _ = synthetic(20_000, 1)
    cal = fit(s, y)
    s2, y2, _ = synthetic(20_000, 2)
    pred = np.array([cal.predict_p(v) for v in s2])
    buckets = reliability(pred, y2)
    assert reliability_ok(buckets, tol=0.05, min_n=100)
    assert brier(pred, y2) < cal.metrics["brier_base"]


def test_prob_info_n_and_low_data():
    s, y, _ = synthetic(300, 3)
    cal = fit(s, y, min_n=100)
    info = cal.prob_info(50.0)
    assert 0 <= info["ci_low"] <= info["p"] <= info["ci_high"] <= 1 or info["low_data"]
    assert info["n"] >= 1
    assert isinstance(info["low_data"], bool)


def test_wilson_and_ev():
    lo, hi = wilson(61, 100)
    assert lo < 0.61 < hi
    assert abs(ev_r(0.61, 1.5, 0.05) - (0.61 * 1.5 - 0.39 - 0.05)) < 1e-12


def test_fit_from_trades_and_db(tmp_settings):
    s, y, _ = synthetic(2_000, 4)
    trades = pl.DataFrame({"score": s, "label": np.where(y > 0, 1, 0).astype(np.int8), "status": np.zeros(len(s), np.int8),
                           "side": np.where(np.arange(len(s)) % 2 == 0, 1, -1).astype(np.int8),
                           "group": ["crypto_major"] * len(s), "tf": ["1h"] * len(s)})
    models = fit_from_trades(trades)
    assert set(models) == {("crypto_major", "long", "1h"), ("crypto_major", "short", "1h")}
    save_models(models, strategy="confluence_v1")
    save_models(models, strategy="confluence_v1")          # новая версия, старая неактивна
    loaded = load_active("confluence_v1")
    assert len(loaded) == 2
    cal = loaded[("crypto_major", "long", "1h")]
    assert 0 <= cal.predict_p(70) <= 1


def test_live_check_flags_stale():
    pred = np.full(200, 0.7)
    obs = np.zeros(200)
    assert live_check(pred, obs)["stale"] is True
    obs2 = (np.random.default_rng(0).random(200) < 0.7).astype(float)
    assert live_check(pred, obs2)["stale"] is False
