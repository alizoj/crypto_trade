"""Фаза 5: DSR/PBO и walk-forward на синтетике, где «правильный» параметр известен."""

from __future__ import annotations

import numpy as np

from quant.backtest.challenge import RiskParams
from quant.backtest.synthetic import fair_tradeset
from quant.config import load_challenge_config
from quant.optimize.stats import deflated_sharpe, pbo_cscv
from quant.optimize.walkforward import WFConfig, run_walkforward


def test_dsr_penalises_many_trials():
    rng = np.random.default_rng(0)
    r = rng.normal(0.05, 1.0, 500)
    few = deflated_sharpe(r, rng.normal(0, 0.05, 2))["dsr"]
    many = deflated_sharpe(r, rng.normal(0, 0.05, 1000))["dsr"]
    assert many < few


def test_pbo_noise_vs_signal():
    rng = np.random.default_rng(1)
    noise = rng.normal(0, 1, (400, 50))
    assert pbo_cscv(noise)["pbo"] > 0.3
    signal = noise.copy()
    signal[:, 7] += 0.5                      # один вариант реально лучше везде
    assert pbo_cscv(signal)["pbo"] < 0.1


def test_walkforward_prefers_real_edge(tmp_settings):
    cfg = load_challenge_config()
    risk = RiskParams(max_concurrent_positions=1, soft_daily_stop=0.0, max_correlated_exposure_pct=0.0,
                      min_notional_usd=0.0)
    cache = {}

    def make(params):
        key = params["edge"]
        if key not in cache:
            cache[key] = fair_tradeset(900, p_win=key, seed=int(key * 1000))
        return cache[key]

    base = {"edge": 0.5}
    ts0 = make(base)
    res = run_walkforward(study="test_wf", strategy="synthetic", make_tradeset=make, base_params=base,
                          search_space={"edge": [0.4, 0.5, 0.6]}, chall_cfg=cfg, risk=risk,
                          wf=WFConfig(horizon_days=45, fail_daily_max=1.0, step_days=3, holdout_months=3,
                                      train_months=4, test_months=2, step_months=2),
                          data_start=ts0.meta["data_start_min"], data_end=ts0.meta["data_end_min"],
                          dataset_hash="synthetic", embargo_min=1440)
    chosen_edges = [res.combos[c]["point"]["edge"] for c in res.chosen]
    assert chosen_edges.count(0.6) >= len(chosen_edges) - 1
    assert res.combos[res.final_combo]["point"]["edge"] == 0.6
    assert res.oos_trades is not None and res.oos_classic["winrate"] > 0.55
    assert 0 <= res.pbo["pbo"] <= 1
    assert "dsr" in res.dsr
