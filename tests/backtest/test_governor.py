"""Live-план (quant.risk.governor) даёт тот же размер, что numba-симулятор при том же состоянии счёта."""

from __future__ import annotations

import numpy as np
import pytest

from quant.backtest.challenge import RiskParams, Stress, stage_params
from quant.backtest.sim import SimArrays, trace_simulate
from quant.config import load_challenge_config
from quant.instruments import get
from quant.propsim import Account
from quant.risk.governor import plan_trade
from quant.timeutil import to_min, utc

START = to_min(utc(2026, 3, 2, 20, 10))


def one_trade(entry=100.0, sl=98.0, side=1, max_lot=250_000.0, max_total=500_000.0):
    n = 1
    return SimArrays(
        t_entry_min=np.array([START + 5]), t_side=np.array([side]), t_entry_px=np.array([entry]),
        t_entry_slip=np.zeros(n), t_entry_liq=np.ones(n, np.int64), t_sl=np.array([sl]),
        t_risk=np.array([abs(entry - sl)]), t_sym=np.zeros(n, np.int64), t_group=np.zeros(n, np.int64),
        t_max_lot=np.array([max_lot]), t_max_total=np.array([max_total]), t_skip=np.zeros(n, np.bool_),
        t_x1_min=np.array([START + 30]), t_x1_px=np.array([entry]), t_x1_slip=np.zeros(n), t_x1_frac=np.ones(n),
        t_x2_min=np.array([-1]), t_x2_px=np.zeros(n), t_x2_slip=np.zeros(n), t_x2_frac=np.zeros(n),
        t_pstart=np.array([0]), t_plen=np.array([2]), p_ts=np.array([START + 5, START + 30]),
        p_wm_lo=np.array([entry, entry]), p_wm_hi=np.array([entry, entry]), p_mc=np.array([entry, entry]),
        p_wl_lo=np.array([entry, entry]), p_wl_hi=np.array([entry, entry]), p_lc=np.array([entry, entry]),
        t_fstart=np.zeros(n, np.int64), t_flen=np.zeros(n, np.int64), f_min=np.zeros(0, np.int64), f_amt=np.zeros(0))


@pytest.mark.parametrize("sl,balance", [(98.0, 10_000), (99.5, 10_000), (90.0, 9_700), (99.9, 10_300)])
def test_plan_matches_sim(sl, balance):
    cfg = load_challenge_config()
    risk = RiskParams(max_correlated_exposure_pct=0.0)
    inst = get("BTCUSDT")
    P = stage_params(cfg, "two_stage", "stage1", 10_000, risk, Stress())
    arr = one_trade(sl=sl, max_lot=inst.max_per_lot_usd, max_total=inst.max_total_usd)
    _, trace = trace_simulate(P, START, 2 * 1440, arr, init_balance=float(balance))
    sim_qty = float(trace[trace[:, 1] == 1][0, 3])
    acc = Account.new(cfg, challenge_type="two_stage", stage="stage1", size=10_000, start_ts=START, balance=balance)
    plan = plan_trade(acc, inst, side=1, entry=100.0, sl=sl, risk=risk)
    assert plan.blocked_reason is None
    assert plan.qty == pytest.approx(sim_qty, rel=1e-9)


def test_plan_blocked_by_soft_stop():
    cfg = load_challenge_config()
    acc = Account.new(cfg, challenge_type="two_stage", stage="stage1", size=10_000, start_ts=START)
    p = acc.open("ETHUSDT", "long", qty=1, price=2_000, ts=START, fee=0.0)
    acc.update_marks({"ETHUSDT": 1_690}, START + 1)            # −310 ≥ 60% от 500
    plan = plan_trade(acc, get("BTCUSDT"), side=1, entry=100, sl=98, risk=RiskParams())
    assert plan.blocked_reason and "soft" in plan.blocked_reason
    assert p.id in acc.positions
