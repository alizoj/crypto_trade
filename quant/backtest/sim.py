"""Ступень B бэктеста: быстрая (numba) симуляция челленджа поверх готовых сделок ступени A.

Правила — те же, что в эталонном quant.propsim.Account (перекрёстная проверка в tests/backtest):
equity по mark (или last в стресс-сценарии), дневной/общий floor, окно сброса, DLP/DLR,
target_lock, внутрибарная проверка по худшей цене. Плюс risk governor (раздел 5).

Размер позиции решается в момент входа по состоянию счёта; траектория цены, выходы и funding
сделки от счёта не зависят и берутся из ступени A.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from numba import njit, prange

INF = np.int64(2 ** 62)
MIN_PER_DAY = 1440

# индексы вектора параметров P
(P_SIZE, P_DAILY, P_MAXLOSS, P_TARGET, P_MINDAYS, P_BOUNDARY, P_WINDOW, P_RESET_MODE, P_TOTAL_MODE,
 P_TRAIL_REF, P_TARGET_METRIC, P_DLP, P_DLR, P_EQ_SRC, P_LIQ, P_MMR, P_MAKER, P_TAKER, P_FEE_MULT,
 P_SLIP_MULT, P_FUND_MULT, P_LEV, P_RISK_FRAC, P_ABS_CAP, P_FIXED_RISK, P_SOFT_STOP, P_MAX_CONC,
 P_CORR_CAP, P_FLATTEN, P_LOCK, P_LOCK_NOTIONAL, P_MIN_NOTIONAL, P_PAYOUT_PERIOD, P_PAYOUT_SHARE,
 P_PAYOUT_CAP, P_PAYOUT_MIN, P_N) = range(37)

# статусы и причины
ST_OPEN, ST_PASS, ST_FAIL = 0, 1, 2
R_NONE, R_DAILY, R_TOTAL, R_LIQ = 0, 1, 2, 3
GROUP_ALT = 1
MAX_POS = 32


@njit(cache=True)
def _day_id(m, boundary):
    return (m + (MIN_PER_DAY - boundary)) // MIN_PER_DAY


@njit(cache=True)
def _day_start(d, boundary):
    return d * MIN_PER_DAY - (MIN_PER_DAY - boundary)


# индексы вектора состояния SV (массив — чтобы вспомогательные функции могли его менять)
(SV_BAL, SV_DSB, SV_OLDF, SV_OLDU, SV_PEAK, SV_STATUS, SV_REASON, SV_DLP, SV_DLR, SV_NOPEN,
 SV_TREACHED, SV_DAYS, SV_N) = range(13)


@njit(cache=True)
def _unreal(pt, pq, pmark, pentry, t_side):
    u = 0.0
    g = 0.0
    for j in range(MAX_POS):
        if pt[j] >= 0:
            u += t_side[pt[j]] * pq[j] * (pmark[j] - pentry[j])
            g += pq[j] * pmark[j]
    return u, g


@njit(cache=True)
def _flatten_floor(SV, floor, pt, pq, pmark, taker):
    """Закрыть всё по цене, соответствующей floor (не лучше), с taker-комиссией."""
    fees = 0.0
    for j in range(MAX_POS):
        if pt[j] >= 0:
            fees += pq[j] * pmark[j] * taker
            pt[j] = -1
    SV[SV_NOPEN] = 0
    SV[SV_BAL] = floor - fees


@njit(cache=True)
def _check(SV, eq, gross, m, update_peak, C, pt, pq, pmark, taker):
    """Проверка лимитов (как Account._check без цели). C = (daily_limit, max_loss, size, trailing,
    trail_bal, liq_on, mmr). Возвращает True, если стадия провалена."""
    daily_limit, max_loss, size, trailing, trail_bal, liq_on, mmr = C[0], C[1], C[2], C[3], C[4], C[5], C[6]
    if update_peak and trailing > 0.5:
        ref = SV[SV_BAL] if trail_bal > 0.5 else eq
        if ref > SV[SV_PEAK]:
            SV[SV_PEAK] = ref
    dfloor = SV[SV_DSB] - daily_limit
    if SV[SV_OLDU] >= 0 and m < SV[SV_OLDU] and SV[SV_OLDF] > dfloor:
        dfloor = SV[SV_OLDF]
    tfloor = (SV[SV_PEAK] - max_loss) if trailing > 0.5 else (size - max_loss)
    if eq <= tfloor + 1e-9:
        _flatten_floor(SV, tfloor, pt, pq, pmark, taker)
        SV[SV_STATUS] = ST_FAIL
        SV[SV_REASON] = R_TOTAL
        return True
    if eq <= dfloor + 1e-9:
        _flatten_floor(SV, dfloor, pt, pq, pmark, taker)
        if SV[SV_DLP] > 0.5 or SV[SV_DLR] > 0.5:
            if SV[SV_DLP] > 0.5:
                SV[SV_DLP] = 0
            else:
                SV[SV_DLR] = 0
            SV[SV_DSB] = SV[SV_BAL]
            SV[SV_OLDU] = -1
            return False
        SV[SV_STATUS] = ST_FAIL
        SV[SV_REASON] = R_DAILY
        return True
    if liq_on > 0.5 and SV[SV_NOPEN] > 0 and eq <= mmr * gross + 1e-9:
        _flatten_floor(SV, eq, pt, pq, pmark, taker)
        SV[SV_STATUS] = ST_FAIL
        SV[SV_REASON] = R_LIQ
        return True
    return False


@njit(cache=True)
def _try_pass(SV, eq, has_target, target, tgt_eq, min_days, pt, pq, pmark, pentry, t_side, taker):
    """Цель стадии (как Account._check_target). Возвращает True при прохождении."""
    if not has_target:
        return False
    metric = eq if tgt_eq else SV[SV_BAL]
    if metric < target - 1e-9:
        return False
    SV[SV_TREACHED] = 1
    if SV[SV_DAYS] < min_days:
        return False
    for j in range(MAX_POS):
        if pt[j] >= 0:
            tr = pt[j]
            SV[SV_BAL] += t_side[tr] * pq[j] * (pmark[j] - pentry[j]) - pq[j] * pmark[j] * taker
            pt[j] = -1
    SV[SV_NOPEN] = 0
    SV[SV_STATUS] = ST_PASS
    return True


@njit(cache=True)
def _full_check(SV, m, update_peak, C, has_target, target, tgt_eq, min_days,
                pt, pq, pmark, pentry, t_side, taker):
    u, g = _unreal(pt, pq, pmark, pentry, t_side)
    if _check(SV, SV[SV_BAL] + u, g, m, update_peak, C, pt, pq, pmark, taker):
        return True
    u, g = _unreal(pt, pq, pmark, pentry, t_side)
    return _try_pass(SV, SV[SV_BAL] + u, has_target, target, tgt_eq, min_days, pt, pq, pmark, pentry, t_side, taker)


@njit(cache=True)
def simulate(P, start, horizon,
             t_entry_min, t_side, t_entry_px, t_entry_slip, t_entry_liq, t_sl, t_risk, t_sym, t_group,
             t_max_lot, t_max_total, t_skip,
             t_x1_min, t_x1_px, t_x1_slip, t_x1_frac, t_x2_min, t_x2_px, t_x2_slip, t_x2_frac,
             t_pstart, t_plen, p_ts, p_wm_lo, p_wm_hi, p_mc, p_wl_lo, p_wl_hi, p_lc,
             t_fstart, t_flen, f_min, f_amt,
             trace, init_balance):
    """Одна стадия с момента start (минуты) на horizon минут.

    Порядок внутри минуты (как у эталонного Account): сброс дня → funding → входы → проверка по худшей
    цене бара → выходы → mark на закрытие минуты. Проверка лимитов и цели — после каждой операции.
    Возвращает (status, end_min, reason, trading_days, balance, n_taken, payouts, max_dd_pct, n_trace).
    trace — массив (N, 6) или (0, 6): minute, kind, trade, qty, balance, equity (для сверки с Account).
    """
    size = P[P_SIZE]
    daily_limit = size * P[P_DAILY]
    max_loss = size * P[P_MAXLOSS]
    has_target = not np.isnan(P[P_TARGET])
    target = size * (1.0 + P[P_TARGET]) if has_target else 0.0
    min_days = int(P[P_MINDAYS])
    boundary = int(P[P_BOUNDARY])
    window = int(P[P_WINDOW])
    reset_mode = int(P[P_RESET_MODE])      # 0 new_day, 1 old_day, 2 both
    tgt_eq = P[P_TARGET_METRIC] > 0.5
    eq_last = P[P_EQ_SRC] > 0.5
    maker = P[P_MAKER] * P[P_FEE_MULT]
    taker = P[P_TAKER] * P[P_FEE_MULT]
    slip_mult = P[P_SLIP_MULT]
    fund_mult = P[P_FUND_MULT]
    lev = P[P_LEV]
    max_conc = min(int(P[P_MAX_CONC]), MAX_POS)
    flatten = P[P_FLATTEN] > 0.5
    lock_on = P[P_LOCK] > 0.5
    payout_period = int(P[P_PAYOUT_PERIOD])
    trailing = P[P_TOTAL_MODE] > 0.5
    C = np.array([daily_limit, max_loss, size, P[P_TOTAL_MODE], P[P_TRAIL_REF], P[P_LIQ], P[P_MMR]])
    n_tr = len(t_entry_min)
    trace_cap = trace.shape[0]
    n_trace = 0

    SV = np.zeros(SV_N)
    SV[SV_BAL] = init_balance
    SV[SV_DSB] = init_balance
    SV[SV_OLDU] = -1
    SV[SV_PEAK] = init_balance
    SV[SV_STATUS] = ST_OPEN
    SV[SV_DLP] = P[P_DLP]
    SV[SV_DLR] = P[P_DLR]
    day = _day_id(start, boundary)
    pending_snap = np.int64(-1)
    last_day = np.int64(-(2 ** 40))
    n_taken = 0
    lock_at = np.int64(-1)
    payouts = 0.0
    next_payout = start + payout_period * MIN_PER_DAY if payout_period > 0 else INF
    eq_peak = init_balance
    max_dd = 0.0

    pt = np.full(MAX_POS, -1, np.int64)
    pq0 = np.zeros(MAX_POS)
    pq = np.zeros(MAX_POS)
    pmark = np.zeros(MAX_POS)
    plc = np.zeros(MAX_POS)
    pptr = np.zeros(MAX_POS, np.int64)
    pend = np.zeros(MAX_POS, np.int64)
    pstage = np.zeros(MAX_POS, np.int64)
    fptr = np.zeros(MAX_POS, np.int64)
    fend = np.zeros(MAX_POS, np.int64)
    pentry = np.zeros(MAX_POS)

    ei = np.searchsorted(t_entry_min, start)
    end = start + horizon
    m = start

    while True:
        # ---------- ближайшее событие
        t_next = end
        if ei < n_tr and t_entry_min[ei] < t_next:
            t_next = t_entry_min[ei]
        nb = _day_start(day + 1, boundary)
        if nb < t_next:
            t_next = nb
        if pending_snap >= 0 and pending_snap < t_next:
            t_next = pending_snap
        if lock_at >= 0 and lock_at < t_next:
            t_next = lock_at
        n_open = int(SV[SV_NOPEN])
        if n_open > 0:
            for j in range(MAX_POS):
                if pt[j] < 0:
                    continue
                tr = pt[j]
                if pptr[j] < pend[j] and p_ts[pptr[j]] < t_next:
                    t_next = p_ts[pptr[j]]
                xm = t_x1_min[tr] if pstage[j] == 0 else t_x2_min[tr]
                if xm >= 0 and xm < t_next:
                    t_next = xm
                if fptr[j] < fend[j] and f_min[fptr[j]] < t_next:
                    t_next = f_min[fptr[j]]
            if flatten:
                fl = nb - 1
                if fl >= m and fl < t_next:
                    t_next = fl
        if payout_period > 0 and n_open == 0 and next_payout < t_next:
            t_next = max(next_payout, m)
        if t_next >= end:
            m = end
            break
        m = t_next

        # ---------- 1. сброс дня (возможно несколько подряд)
        while _day_start(day + 1, boundary) <= m:
            ds = _day_start(day + 1, boundary)
            of = SV[SV_DSB] - daily_limit
            day += 1
            if reset_mode == 1:
                pending_snap = ds + window
                SV[SV_OLDU] = -1
            else:
                SV[SV_DSB] = SV[SV_BAL]
                pending_snap = -1
                if reset_mode == 2:
                    SV[SV_OLDF] = of
                    SV[SV_OLDU] = ds + window
                else:
                    SV[SV_OLDU] = -1
            if lock_on and SV[SV_TREACHED] > 0.5 and SV[SV_DAYS] < min_days:
                lock_at = ds + window
        # ---------- 2. отложенная фиксация (old_day)
        if pending_snap >= 0 and m >= pending_snap:
            SV[SV_DSB] = SV[SV_BAL]
            pending_snap = -1

        # ---------- 3. funding (проверка после каждого начисления)
        done = False
        if SV[SV_NOPEN] > 0:
            for j in range(MAX_POS):
                if pt[j] < 0:
                    continue
                applied = False
                while fptr[j] < fend[j] and f_min[fptr[j]] == m:
                    SV[SV_BAL] += f_amt[fptr[j]] * pq[j] * fund_mult
                    fptr[j] += 1
                    applied = True
                if applied and _full_check(SV, m, True, C, has_target, target, tgt_eq, min_days,
                                           pt, pq, pmark, pentry, t_side, taker):
                    done = True
                    break
        if done:
            break

        # ---------- 4. target_lock: минимальная позиция ради торгового дня
        if lock_at >= 0 and m >= lock_at:
            lock_at = -1
            if SV[SV_TREACHED] > 0.5 and SV[SV_DAYS] < min_days:
                SV[SV_BAL] -= 2.0 * P[P_LOCK_NOTIONAL] * taker
                d = _day_id(m, boundary)
                if d != last_day:
                    SV[SV_DAYS] += 1
                    last_day = d
                if _full_check(SV, m, True, C, has_target, target, tgt_eq, min_days,
                               pt, pq, pmark, pentry, t_side, taker):
                    break

        # ---------- 5. входы (risk governor)
        while ei < n_tr and t_entry_min[ei] == m:
            i = ei
            ei += 1
            if SV[SV_STATUS] != ST_OPEN or t_skip[i]:
                continue
            if lock_on and SV[SV_TREACHED] > 0.5 and SV[SV_DAYS] < min_days:
                continue
            unreal = 0.0
            gross = 0.0
            sym_notional = 0.0
            alt_risk = 0.0
            opposite = False
            for j in range(MAX_POS):
                if pt[j] < 0:
                    continue
                tr = pt[j]
                unreal += t_side[tr] * pq[j] * (pmark[j] - pentry[j])
                gross += pq[j] * pmark[j]
                if t_sym[tr] == t_sym[i]:
                    sym_notional += pq[j] * pmark[j]
                    if t_side[tr] != t_side[i]:
                        opposite = True
                if t_group[tr] == GROUP_ALT:
                    alt_risk += pq[j] * t_risk[tr]
            eq = SV[SV_BAL] + unreal
            if opposite or int(SV[SV_NOPEN]) >= max_conc:
                continue
            dfloor = SV[SV_DSB] - daily_limit
            if SV[SV_OLDU] >= 0 and m < SV[SV_OLDU] and SV[SV_OLDF] > dfloor:
                dfloor = SV[SV_OLDF]
            tfloor = (SV[SV_PEAK] - max_loss) if trailing else (size - max_loss)
            if P[P_SOFT_STOP] > 0 and (SV[SV_DSB] - eq) >= P[P_SOFT_STOP] * daily_limit:
                continue
            room = min(eq - dfloor, eq - tfloor)
            if room <= 0:
                continue
            if P[P_FIXED_RISK] > 0:
                risk_usd = P[P_FIXED_RISK] * size
            else:
                risk_usd = min(P[P_RISK_FRAC] * room, P[P_ABS_CAP] * size)
            entry = t_entry_px[i] + t_side[i] * (slip_mult - 1.0) * t_entry_slip[i]
            fee_in = maker if t_entry_liq[i] == 0 else taker
            per_unit = abs(entry - t_sl[i]) + entry * (fee_in + taker) + t_entry_slip[i] * slip_mult
            if per_unit <= 0:
                continue
            notional = risk_usd / per_unit * entry
            if notional > t_max_lot[i]:
                notional = t_max_lot[i]
            cap_total = t_max_total[i] - sym_notional
            if notional > cap_total:
                notional = cap_total
            cap_lev = lev * eq - gross
            if notional > cap_lev:
                notional = cap_lev
            if t_group[i] == GROUP_ALT and P[P_CORR_CAP] > 0:
                allowed = P[P_CORR_CAP] * size - alt_risk
                if allowed <= 0:
                    continue
                lim_n = allowed / t_risk[i] * entry
                if notional > lim_n:
                    notional = lim_n
            if notional < P[P_MIN_NOTIONAL] or notional <= 0:
                continue
            qty = notional / entry
            slot = -1
            for j in range(MAX_POS):
                if pt[j] < 0:
                    slot = j
                    break
            pt[slot] = i
            pq0[slot] = qty
            pq[slot] = qty
            pmark[slot] = entry
            plc[slot] = entry
            pentry[slot] = entry
            pptr[slot] = t_pstart[i]
            pend[slot] = t_pstart[i] + t_plen[i]
            pstage[slot] = 0
            fptr[slot] = t_fstart[i]
            fend[slot] = t_fstart[i] + t_flen[i]
            SV[SV_NOPEN] += 1
            n_taken += 1
            SV[SV_BAL] -= notional * fee_in
            d = _day_id(m, boundary)
            if d != last_day:
                SV[SV_DAYS] += 1
                last_day = d
            if n_trace < trace_cap:
                trace[n_trace, 0] = m; trace[n_trace, 1] = 1; trace[n_trace, 2] = i
                trace[n_trace, 3] = qty; trace[n_trace, 4] = SV[SV_BAL]; trace[n_trace, 5] = eq - notional * fee_in
                n_trace += 1
            if _full_check(SV, m, True, C, has_target, target, tgt_eq, min_days,
                           pt, pq, pmark, pentry, t_side, taker):
                break
        if SV[SV_STATUS] != ST_OPEN:
            break

        # ---------- 6. внутрибарная проверка по худшей цене (без обновления пика)
        if SV[SV_NOPEN] > 0:
            unreal_w = 0.0
            gross = 0.0
            for j in range(MAX_POS):
                if pt[j] < 0:
                    continue
                tr = pt[j]
                s = t_side[tr]
                worst = pmark[j]
                if pptr[j] < pend[j] and p_ts[pptr[j]] == m:
                    k = pptr[j]
                    if eq_last:
                        worst = p_wl_lo[k] if s > 0 else p_wl_hi[k]
                    else:
                        worst = p_wm_lo[k] if s > 0 else p_wm_hi[k]
                unreal_w += s * pq[j] * (worst - pentry[j])
                gross += pq[j] * pmark[j]
            eq_w = SV[SV_BAL] + unreal_w
            if _check(SV, eq_w, gross, m, False, C, pt, pq, pmark, taker):
                break
            if _try_pass(SV, eq_w, has_target, target, tgt_eq, min_days, pt, pq, pmark, pentry, t_side, taker):
                break

        # ---------- 7. выходы (проверка после каждого)
        done = False
        for j in range(MAX_POS):
            if pt[j] < 0:
                continue
            tr = pt[j]
            s = t_side[tr]
            if pstage[j] == 0:
                xm = t_x1_min[tr]; xp = t_x1_px[tr]; xs = t_x1_slip[tr]; xf = t_x1_frac[tr]
            else:
                xm = t_x2_min[tr]; xp = t_x2_px[tr]; xs = t_x2_slip[tr]; xf = t_x2_frac[tr]
            if xm != m:
                continue
            px = xp - s * (slip_mult - 1.0) * xs
            final = pstage[j] == 1 or t_x2_min[tr] < 0
            q = pq[j] if final else min(xf * pq0[j], pq[j])
            SV[SV_BAL] += s * q * (px - pentry[j]) - q * px * taker
            pq[j] -= q
            if n_trace < trace_cap:
                trace[n_trace, 0] = m; trace[n_trace, 1] = 2; trace[n_trace, 2] = tr
                trace[n_trace, 3] = q; trace[n_trace, 4] = SV[SV_BAL]; trace[n_trace, 5] = 0.0
                n_trace += 1
            if not final and pq[j] > 1e-15:
                pstage[j] = 1
            else:
                pt[j] = -1
                SV[SV_NOPEN] -= 1
            if _full_check(SV, m, True, C, has_target, target, tgt_eq, min_days,
                           pt, pq, pmark, pentry, t_side, taker):
                done = True
                break
        if done:
            break

        # ---------- 8. закрыть всё перед окном сброса (flatten_before_reset)
        if flatten and SV[SV_NOPEN] > 0 and m == _day_start(day + 1, boundary) - 1:
            for j in range(MAX_POS):
                if pt[j] < 0:
                    continue
                tr = pt[j]
                px = plc[j]
                if pptr[j] < pend[j] and p_ts[pptr[j]] == m:
                    px = p_lc[pptr[j]]
                SV[SV_BAL] += t_side[tr] * pq[j] * (px - pentry[j]) - pq[j] * px * taker
                pt[j] = -1
            SV[SV_NOPEN] = 0

        # ---------- 9. mark на закрытие минуты
        for j in range(MAX_POS):
            if pt[j] < 0:
                continue
            while pptr[j] < pend[j] and p_ts[pptr[j]] <= m:
                if p_ts[pptr[j]] == m:
                    pmark[j] = p_mc[pptr[j]]
                    plc[j] = p_lc[pptr[j]]
                pptr[j] += 1
        if _full_check(SV, m, True, C, has_target, target, tgt_eq, min_days,
                       pt, pq, pmark, pentry, t_side, taker):
            break

        # ---------- 10. просадка и выплаты (стадия Трейдер)
        u, g = _unreal(pt, pq, pmark, pentry, t_side)
        eq = SV[SV_BAL] + u
        if eq > eq_peak:
            eq_peak = eq
        dd = (eq_peak - eq) / size
        if dd > max_dd:
            max_dd = dd
        if payout_period > 0 and SV[SV_NOPEN] == 0 and m >= next_payout:
            profit = SV[SV_BAL] - size
            if profit > 0:
                gross_w = min(profit, P[P_PAYOUT_CAP] / P[P_PAYOUT_SHARE])
                if P[P_PAYOUT_SHARE] * gross_w >= P[P_PAYOUT_MIN]:
                    payouts += P[P_PAYOUT_SHARE] * gross_w
                    SV[SV_BAL] -= gross_w
                    SV[SV_DSB] -= gross_w       # ASSUMPTION: вывод прибыли не считается дневной потерей
            next_payout = m + payout_period * MIN_PER_DAY

    return (int(SV[SV_STATUS]), m, int(SV[SV_REASON]), int(SV[SV_DAYS]), SV[SV_BAL], n_taken, payouts,
            max_dd, n_trace)


@njit(cache=True, parallel=True)
def run_chain(starts, horizons, P_stages, n_stages, init_balance,
              t_entry_min, t_side, t_entry_px, t_entry_slip, t_entry_liq, t_sl, t_risk, t_sym, t_group,
              t_max_lot, t_max_total, t_skip,
              t_x1_min, t_x1_px, t_x1_slip, t_x1_frac, t_x2_min, t_x2_px, t_x2_slip, t_x2_frac,
              t_pstart, t_plen, p_ts, p_wm_lo, p_wm_hi, p_mc, p_wl_lo, p_wl_hi, p_lc,
              t_fstart, t_flen, f_min, f_amt):
    """Для каждого старта — стадии подряд (stage1 → stage2 → trader). Стадия k+1 стартует со следующей
    минуты после прохождения стадии k.

    Выход res[s, k, :] = status, start, end, reason, days, balance, n_taken, payouts, max_dd.
    """
    ns = len(starts)
    res = np.full((ns, n_stages, 9), np.nan)
    empty = np.zeros((0, 6))
    for si in prange(ns):
        st = starts[si]
        for k in range(n_stages):
            bal0 = P_stages[k][P_SIZE]
            if init_balance > 0:
                bal0 = init_balance
            out = simulate(P_stages[k], st, horizons[k],
                           t_entry_min, t_side, t_entry_px, t_entry_slip, t_entry_liq, t_sl, t_risk, t_sym, t_group,
                           t_max_lot, t_max_total, t_skip,
                           t_x1_min, t_x1_px, t_x1_slip, t_x1_frac, t_x2_min, t_x2_px, t_x2_slip, t_x2_frac,
                           t_pstart, t_plen, p_ts, p_wm_lo, p_wm_hi, p_mc, p_wl_lo, p_wl_hi, p_lc,
                           t_fstart, t_flen, f_min, f_amt, empty, bal0)
            status, end_m, reason, days, bal, n_taken, payouts, max_dd, _ = out
            res[si, k, 0] = status
            res[si, k, 1] = st
            res[si, k, 2] = end_m
            res[si, k, 3] = reason
            res[si, k, 4] = days
            res[si, k, 5] = bal
            res[si, k, 6] = n_taken
            res[si, k, 7] = payouts
            res[si, k, 8] = max_dd
            if status != ST_PASS:
                break
            st = end_m + 1
    return res


@dataclass
class SimArrays:
    """Плоские массивы сделок для ступени B (только исполненные сделки, по времени входа)."""

    t_entry_min: np.ndarray
    t_side: np.ndarray
    t_entry_px: np.ndarray
    t_entry_slip: np.ndarray
    t_entry_liq: np.ndarray
    t_sl: np.ndarray
    t_risk: np.ndarray
    t_sym: np.ndarray
    t_group: np.ndarray
    t_max_lot: np.ndarray
    t_max_total: np.ndarray
    t_skip: np.ndarray
    t_x1_min: np.ndarray
    t_x1_px: np.ndarray
    t_x1_slip: np.ndarray
    t_x1_frac: np.ndarray
    t_x2_min: np.ndarray
    t_x2_px: np.ndarray
    t_x2_slip: np.ndarray
    t_x2_frac: np.ndarray
    t_pstart: np.ndarray
    t_plen: np.ndarray
    p_ts: np.ndarray
    p_wm_lo: np.ndarray
    p_wm_hi: np.ndarray
    p_mc: np.ndarray
    p_wl_lo: np.ndarray
    p_wl_hi: np.ndarray
    p_lc: np.ndarray
    t_fstart: np.ndarray
    t_flen: np.ndarray
    f_min: np.ndarray
    f_amt: np.ndarray

    def args(self) -> tuple:
        return (self.t_entry_min, self.t_side, self.t_entry_px, self.t_entry_slip, self.t_entry_liq, self.t_sl,
                self.t_risk, self.t_sym, self.t_group, self.t_max_lot, self.t_max_total, self.t_skip,
                self.t_x1_min, self.t_x1_px, self.t_x1_slip, self.t_x1_frac,
                self.t_x2_min, self.t_x2_px, self.t_x2_slip, self.t_x2_frac,
                self.t_pstart, self.t_plen, self.p_ts, self.p_wm_lo, self.p_wm_hi, self.p_mc,
                self.p_wl_lo, self.p_wl_hi, self.p_lc, self.t_fstart, self.t_flen, self.f_min, self.f_amt)

    def with_skip(self, skip: np.ndarray) -> "SimArrays":
        from dataclasses import replace

        return replace(self, t_skip=skip.astype(np.bool_))

    @property
    def n(self) -> int:
        return len(self.t_entry_min)


def trace_simulate(P: np.ndarray, start: int, horizon: int, arr: SimArrays, cap: int = 100_000,
                   init_balance: float | None = None):
    """Одна стадия с журналом действий (для сверки с Account и отладки)."""
    trace = np.zeros((cap, 6))
    out = simulate(P, start, horizon, *arr.args(), trace, P[P_SIZE] if init_balance is None else init_balance)
    return out, trace[: out[-1]]
