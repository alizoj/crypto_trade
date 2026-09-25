from __future__ import annotations

import numpy as np

from quant.backtest.challenge import RiskParams, evaluate
from quant.backtest.synthetic import fair_trades
from quant.config import load_challenge_config
from quant.montecarlo.bootstrap import bootstrap_ci, resample


def _one_history(n_days: int = 200):
    arr, starts, seg = fair_trades(1, n_days * 9, seed=4)
    return arr, int(starts[0]), int(starts[0] + seg)


def test_resample_keeps_trades_consistent():
    arr, s, e = _one_history()
    b = resample(arr, data_start_min=s, data_end_min=e, rng=np.random.default_rng(1))
    assert b.n > 0
    assert np.all(np.diff(b.t_entry_min) >= 0)
    for i in range(0, b.n, 97):
        p = b.p_ts[b.t_pstart[i]:b.t_pstart[i] + b.t_plen[i]]
        assert p[0] == b.t_entry_min[i]
        assert p[-1] == b.t_x1_min[i]


def test_bootstrap_ci_contains_point_estimate():
    arr, s, e = _one_history(400)
    cfg = load_challenge_config()
    risk = RiskParams(fixed_risk_pct=0.005, max_concurrent_positions=1, soft_daily_stop=0.0,
                      max_correlated_exposure_pct=0.0, target_lock=False, min_notional_usd=0.0)
    point = evaluate(arr, cfg, risk=risk, horizon_days=60, data_start_min=s, data_end_min=e,
                     include_trader=False).metrics
    ci = bootstrap_ci(arr, cfg, data_start_min=s, data_end_min=e, n_boot=30, risk=risk, horizon_days=60,
                      metrics=("p_pass_s1",))
    c = ci["p_pass_s1"]
    assert c["lo"] <= c["hi"]
    assert c["lo"] - 0.15 <= point["p_pass_s1"] <= c["hi"] + 0.15
