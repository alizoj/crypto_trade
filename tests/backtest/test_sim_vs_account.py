"""Перекрёстная проверка: numba-симулятор ступени B против эталонного propsim.Account.

Одинаковые сделки (вход, путь худших цен, частичные выходы, funding) проигрываются в обоих движках;
размер позиции берётся из журнала симулятора. Баланс, статус и причина должны совпасть.
"""

from __future__ import annotations

import numpy as np
import pytest

from quant.backtest import sim as S
from quant.backtest.challenge import RiskParams, Stress, stage_params
from quant.backtest.sim import SimArrays, trace_simulate
from quant.config import load_challenge_config
from quant.propsim import Account
from quant.timeutil import MIN_PER_DAY, to_min, utc

START = to_min(utc(2026, 3, 2, 20, 10))


def make_scenario(seed: int, n_trades: int = 40, n_sym: int = 4):
    rng = np.random.default_rng(seed)
    entries = np.sort(START + rng.integers(1, 6 * MIN_PER_DAY, size=n_trades))
    rows = []
    p_ts, lo, hi, mc = [], [], [], []
    f_min, f_amt = [], []
    for i, e in enumerate(entries):
        side = int(rng.choice([1, -1]))
        sym = i % n_sym
        px = 100.0 + rng.normal(0, 1)
        risk = abs(rng.normal(1.0, 0.3)) + 0.2
        sl = px - side * risk
        hold = int(rng.integers(5, 600))
        two = rng.random() < 0.4
        # путь: случайное блуждание по минутам
        start_p = len(p_ts)
        price = px
        path_min = e + np.arange(hold + 1)
        for k, m in enumerate(path_min):
            step = rng.normal(0, 0.15)
            price = price + step
            w_lo = price - abs(rng.normal(0, 0.1))
            w_hi = price + abs(rng.normal(0, 0.1))
            p_ts.append(int(m)); lo.append(w_lo); hi.append(w_hi); mc.append(price)
        exit_px = mc[-1]
        # последняя минута пути — выход: худшая = цена выхода
        lo[-1] = hi[-1] = exit_px
        x1_min = int(path_min[len(path_min) // 2]) if two else int(path_min[-1])
        x1_px = mc[start_p + len(path_min) // 2] if two else exit_px
        x1_frac = 0.5 if two else 1.0
        fstart = len(f_min)
        for fm in range(int(e) + 1, int(path_min[-1]) + 1):
            if fm % 240 == 0:
                mark = mc[start_p + (fm - int(e))]
                rate = rng.normal(0, 0.0005)
                f_min.append(fm)
                f_amt.append(-side * mark * rate)
        rows.append(dict(entry=int(e), side=side, px=px, sl=sl, risk=risk, sym=sym,
                         x1_min=x1_min, x1_px=x1_px, x1_frac=x1_frac,
                         x2_min=int(path_min[-1]) if two else -1, x2_px=exit_px if two else 0.0,
                         x2_frac=0.5 if two else 0.0, pstart=start_p, plen=len(path_min),
                         fstart=fstart, flen=len(f_min) - fstart))
    n = len(rows)
    col = lambda k, dt=np.float64: np.array([r[k] for r in rows], dtype=dt)  # noqa: E731
    arr = SimArrays(
        t_entry_min=col("entry", np.int64), t_side=col("side", np.int64), t_entry_px=col("px"),
        t_entry_slip=np.zeros(n), t_entry_liq=np.ones(n, np.int64), t_sl=col("sl"), t_risk=col("risk"),
        t_sym=col("sym", np.int64), t_group=np.zeros(n, np.int64), t_max_lot=np.full(n, 1e12),
        t_max_total=np.full(n, 1e12), t_skip=np.zeros(n, np.bool_),
        t_x1_min=col("x1_min", np.int64), t_x1_px=col("x1_px"), t_x1_slip=np.zeros(n), t_x1_frac=col("x1_frac"),
        t_x2_min=col("x2_min", np.int64), t_x2_px=col("x2_px"), t_x2_slip=np.zeros(n), t_x2_frac=col("x2_frac"),
        t_pstart=col("pstart", np.int64), t_plen=col("plen", np.int64),
        p_ts=np.array(p_ts, np.int64), p_wm_lo=np.array(lo), p_wm_hi=np.array(hi), p_mc=np.array(mc),
        p_wl_lo=np.array(lo), p_wl_hi=np.array(hi), p_lc=np.array(mc),
        t_fstart=col("fstart", np.int64), t_flen=col("flen", np.int64),
        f_min=np.array(f_min, np.int64), f_amt=np.array(f_amt),
    )
    return arr


def replay_account(cfg, arr: SimArrays, trace: np.ndarray, size: float, stage: str = "stage1") -> Account:
    qty = {int(r[2]): float(r[3]) for r in trace if int(r[1]) == 1}
    limits = {f"S{arr.t_sym[i]}#{i}": (1e12, 1e12) for i in range(arr.n)}
    acc = Account.new(cfg, challenge_type="two_stage", stage=stage, size=size, start_ts=START, limits=limits)
    # все минуты событий взятых сделок
    minutes = set()
    for i in qty:
        minutes.update(arr.p_ts[arr.t_pstart[i]:arr.t_pstart[i] + arr.t_plen[i]].tolist())
        minutes.update(arr.f_min[arr.t_fstart[i]:arr.t_fstart[i] + arr.t_flen[i]].tolist())
    # границы дней тоже, чтобы сбросы шли в том же порядке
    pos_of: dict[int, int] = {}
    ptr = {i: arr.t_pstart[i] for i in qty}
    fptr = {i: arr.t_fstart[i] for i in qty}
    for m in sorted(minutes):
        if acc.status != "active":
            break
        acc.advance(int(m))
        # funding
        for i, pid in list(pos_of.items()):
            while fptr[i] < arr.t_fstart[i] + arr.t_flen[i] and arr.f_min[fptr[i]] == m:
                pos = acc.positions[pid]
                amt = arr.f_amt[fptr[i]] * pos.qty
                acc.balance += amt
                acc._check(int(m))
                fptr[i] += 1
            if acc.status != "active":
                break
        if acc.status != "active":
            break
        # входы
        for i in sorted(qty):
            if arr.t_entry_min[i] == m and i not in pos_of:
                p = acc.open(f"S{arr.t_sym[i]}#{i}", int(arr.t_side[i]), qty[i], float(arr.t_entry_px[i]), int(m),
                             liquidity="taker")
                pos_of[i] = p.id
        if acc.status != "active":
            break
        # худшая цена
        worst = {}
        for i, pid in pos_of.items():
            k = ptr[i]
            if k < arr.t_pstart[i] + arr.t_plen[i] and arr.p_ts[k] == m:
                worst[acc.positions[pid].symbol] = (float(arr.p_wm_lo[k]), float(arr.p_wm_hi[k]))
        if pos_of:
            acc.check_intrabar(int(m), worst_mark=worst)
        if acc.status != "active":
            break
        # выходы
        for i, pid in list(pos_of.items()):
            stage2 = acc.positions[pid].qty < qty[i] - 1e-12
            xm = arr.t_x2_min[i] if stage2 else arr.t_x1_min[i]
            if xm != m:
                continue
            final = stage2 or arr.t_x2_min[i] < 0
            xp = arr.t_x2_px[i] if stage2 else arr.t_x1_px[i]
            q = None if final else arr.t_x1_frac[i] * qty[i]
            acc.close(pid, float(xp), int(m), qty=q, liquidity="taker")
            if acc.status != "active":
                break
            if final:
                del pos_of[i]
        if acc.status != "active":
            break
        # mark на закрытие минуты
        marks = {}
        for i, pid in pos_of.items():
            while ptr[i] < arr.t_pstart[i] + arr.t_plen[i] and arr.p_ts[ptr[i]] <= m:
                if arr.p_ts[ptr[i]] == m:
                    marks[acc.positions[pid].symbol] = float(arr.p_mc[ptr[i]])
                ptr[i] += 1
        if marks:
            acc.update_marks(marks, int(m))
    return acc


@pytest.mark.parametrize("seed", range(12))
@pytest.mark.parametrize("mode", ["new_day", "both"])
def test_sim_matches_account(seed, mode):
    cfg = load_challenge_config().with_options(reset_window_mode=mode, liquidation={"enabled": False})
    size = 10_000
    risk = RiskParams(risk_per_trade=0.35, abs_risk_cap_pct=0.02, soft_daily_stop=0.0, max_concurrent_positions=4,
                      max_correlated_exposure_pct=0.0, target_lock=False, min_notional_usd=0.0)
    P = stage_params(cfg, "two_stage", "stage1", size, risk, Stress())
    arr = make_scenario(seed)
    out, trace = trace_simulate(P, START, 8 * MIN_PER_DAY, arr)
    status, end_m, reason, days, balance, n_taken, *_ = out
    # переименуем символы: одна сделка — один символ в Account (funding считается на сделку)
    acc = replay_account(cfg, arr, trace, size)
    assert n_taken > 0
    exp_status = {S.ST_OPEN: "active", S.ST_PASS: "passed", S.ST_FAIL: "failed"}[status]
    assert acc.status == exp_status, (acc.status, exp_status, acc.fail_reason, reason)
    if exp_status == "failed":
        assert acc.fail_reason == {S.R_DAILY: "daily", S.R_TOTAL: "total", S.R_LIQ: "liquidation"}[reason]
    assert acc.balance == pytest.approx(balance, rel=1e-9, abs=1e-6)
    assert acc.trading_days_count == days
