"""Оценка стратегии как прохождения челленджа: rolling start по истории, метрики раздела 7.

P(pass) определяется только внутри горизонта H: исходы pass / fail / unresolved.
Стартовые дни берутся только такие, после которых в данных есть ≥ H дней (без обрезанных прогонов).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from quant.backtest import sim as S
from quant.backtest.sim import SimArrays, run_chain
from quant.config import ChallengeConfig
from quant.timeutil import MIN_PER_DAY, prop_day_id, prop_day_start_min


@dataclass
class RiskParams:
    risk_per_trade: float = 0.10
    abs_risk_cap_pct: float = 0.01
    fixed_risk_pct: float = 0.0          # >0 → фиксированный риск (санити-чек, сравнение режимов)
    soft_daily_stop: float = 0.60
    max_concurrent_positions: int = 3
    max_correlated_exposure_pct: float = 0.02
    flatten_before_reset: bool = False
    target_lock: bool = True
    target_lock_notional_usd: float = 100.0
    min_notional_usd: float = 10.0

    @classmethod
    def from_config(cls, risk: dict | None, **overrides: Any) -> "RiskParams":
        r = dict(risk or {})
        p = cls(**{k: type(getattr(cls, k))(v) if not isinstance(getattr(cls, k), bool) else bool(v)
                   for k, v in r.items() if hasattr(cls, k)})
        for k, v in overrides.items():
            setattr(p, k, v)
        return p


@dataclass
class Stress:
    fee_mult: float = 1.0
    slip_mult: float = 1.0
    fund_mult: float = 1.0
    skip_frac: float = 0.0
    equity_last: bool = False
    seed: int = 11
    name: str = "base"


STRESS_PRESETS = {
    "base": Stress(),
    "fees_x1.5": Stress(fee_mult=1.5, name="fees_x1.5"),
    "slippage_x2": Stress(slip_mult=2.0, name="slippage_x2"),
    "skip_10pct": Stress(skip_frac=0.10, name="skip_10pct"),
    "equity_last": Stress(equity_last=True, name="equity_last"),
    "funding_x1.5": Stress(fund_mult=1.5, name="funding_x1.5"),
}


def stage_params(cfg: ChallengeConfig, challenge_type: str, stage: str, size: float, risk: RiskParams,
                 stress: Stress | None = None, market: str = "crypto_tradfi") -> np.ndarray:
    st = stress or Stress()
    mk = cfg.market(market)
    spec = cfg.stage(challenge_type, stage)
    o = cfg.options
    P = np.zeros(S.P_N)
    P[S.P_SIZE] = size
    P[S.P_DAILY] = spec.daily_loss_pct
    P[S.P_MAXLOSS] = spec.max_loss_pct
    P[S.P_TARGET] = spec.target_pct if spec.target_pct is not None else np.nan
    P[S.P_MINDAYS] = spec.min_trading_days
    P[S.P_BOUNDARY] = mk.day_boundary_min
    P[S.P_WINDOW] = mk.reset_window_minutes
    P[S.P_RESET_MODE] = {"new_day": 0, "old_day": 1, "both": 2}[o.reset_window_mode]
    P[S.P_TOTAL_MODE] = 1.0 if o.total_floor_mode == "trailing" else 0.0
    P[S.P_TRAIL_REF] = 1.0 if o.trailing_reference == "balance" else 0.0
    P[S.P_TARGET_METRIC] = 1.0 if o.target_metric == "equity" else 0.0
    P[S.P_DLP] = float(o.daily_loss_protection)
    P[S.P_DLR] = float(o.daily_loss_reset and stage != "trader")
    P[S.P_EQ_SRC] = 1.0 if (st.equity_last or o.equity_price_source == "last") else 0.0
    P[S.P_LIQ] = float(o.liquidation.enabled)
    P[S.P_MMR] = o.liquidation.maintenance_margin_rate
    P[S.P_MAKER] = mk.fees.maker
    P[S.P_TAKER] = mk.fees.taker
    P[S.P_FEE_MULT] = st.fee_mult
    P[S.P_SLIP_MULT] = st.slip_mult
    P[S.P_FUND_MULT] = st.fund_mult
    P[S.P_LEV] = mk.max_leverage
    P[S.P_RISK_FRAC] = risk.risk_per_trade
    P[S.P_ABS_CAP] = risk.abs_risk_cap_pct
    P[S.P_FIXED_RISK] = risk.fixed_risk_pct
    P[S.P_SOFT_STOP] = risk.soft_daily_stop
    P[S.P_MAX_CONC] = risk.max_concurrent_positions
    P[S.P_CORR_CAP] = risk.max_correlated_exposure_pct
    P[S.P_FLATTEN] = float(risk.flatten_before_reset)
    P[S.P_LOCK] = float(risk.target_lock)
    P[S.P_LOCK_NOTIONAL] = risk.target_lock_notional_usd
    P[S.P_MIN_NOTIONAL] = risk.min_notional_usd
    if stage == "trader":
        P[S.P_PAYOUT_PERIOD] = cfg.payouts.period_days
        P[S.P_PAYOUT_SHARE] = cfg.payouts.profit_share
        P[S.P_PAYOUT_CAP] = cfg.payouts.max_usd_per_period
        P[S.P_PAYOUT_MIN] = cfg.payouts.min_usd
    return P


def rolling_starts(data_start_min: int, data_end_min: int, horizon_days: float, *, boundary_min: int = 1200,
                   window_min: int = 10, step_days: int = 1) -> np.ndarray:
    """Старты в начале каждого проп-дня (после окна сброса), только с ≥ H дней данных впереди."""
    d0 = prop_day_id(data_start_min, boundary_min) + 1
    last = data_end_min - int(horizon_days * MIN_PER_DAY)
    out = []
    d = d0
    while True:
        s = prop_day_start_min(d, boundary_min) + window_min
        if s > last:
            break
        out.append(s)
        d += step_days
    return np.array(out, dtype=np.int64)


@dataclass
class ChallengeResult:
    stages: list[str]
    starts: np.ndarray
    res: np.ndarray                  # (n_starts, n_stages, 9)
    horizons_days: list[float]
    data_end_min: int
    size: float
    challenge_fee_usd: float | None
    metrics: dict = field(default_factory=dict)


def evaluate(arr: SimArrays, cfg: ChallengeConfig, *, challenge_type: str = "two_stage", size: float = 10_000,
             risk: RiskParams | None = None, stress: Stress | None = None, horizon_days: float = 90,
             trader_horizon_days: float = 180, data_start_min: int, data_end_min: int,
             include_trader: bool = True, step_days: int = 1, challenge_fee_usd: float | None = None,
             starts: np.ndarray | None = None) -> ChallengeResult:
    risk = risk or RiskParams()
    st = stress or Stress()
    stages = cfg.stage_names(challenge_type) + (["trader"] if include_trader else [])
    horizons = [horizon_days] * (len(stages) - (1 if include_trader else 0)) + ([trader_horizon_days] if include_trader else [])
    P = np.stack([stage_params(cfg, challenge_type, s, size, risk, st) for s in stages])
    mk = cfg.market("crypto_tradfi")
    if starts is None:
        starts = rolling_starts(data_start_min, data_end_min, horizon_days, boundary_min=mk.day_boundary_min,
                                window_min=mk.reset_window_minutes, step_days=step_days)
    if st.skip_frac > 0:
        rng = np.random.default_rng(st.seed)
        arr = arr.with_skip(rng.random(arr.n) < st.skip_frac)
    hz = np.array([int(h * MIN_PER_DAY) for h in horizons], dtype=np.int64)
    res = run_chain(starts, hz, P, len(stages), -1.0, *arr.args()) if len(starts) else np.zeros((0, len(stages), 9))
    out = ChallengeResult(stages=stages, starts=starts, res=res, horizons_days=horizons, data_end_min=data_end_min,
                          size=size, challenge_fee_usd=challenge_fee_usd)
    out.metrics = challenge_metrics(out)
    return out


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * np.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (c - h, c + h)


def challenge_metrics(r: ChallengeResult) -> dict:
    res, starts = r.res, r.starts
    m: dict[str, Any] = {"n_starts": int(len(starts))}
    if len(starts) == 0:
        return m
    eval_stages = [s for s in r.stages if s != "trader"]
    cum_h = 0.0
    both_mask = np.ones(len(starts), dtype=bool)
    for k, name in enumerate(eval_stages):
        cum_h += r.horizons_days[k]
        # для стадии k — только старты, у которых впереди ≥ сумма горизонтов до k включительно
        ok = starts + int(cum_h * MIN_PER_DAY) <= r.data_end_min
        prev_pass = np.ones(len(starts), dtype=bool) if k == 0 else (res[:, k - 1, 0] == S.ST_PASS)
        sel = ok & prev_pass
        st = res[sel, k, 0]
        n = int(sel.sum())
        tag = f"s{k + 1}"
        if n:
            n_pass = int((st == S.ST_PASS).sum())
            n_fail = int((st == S.ST_FAIL).sum())
            reason = res[sel, k, 3]
            m[f"p_pass_{tag}_cond"] = n_pass / n
            m[f"n_{tag}"] = n
            m[f"p_fail_{tag}_cond"] = n_fail / n
            m[f"p_fail_daily_{tag}"] = float(((st == S.ST_FAIL) & (reason == S.R_DAILY)).sum() / n)
            m[f"p_fail_total_{tag}"] = float(((st == S.ST_FAIL) & (reason == S.R_TOTAL)).sum() / n)
            m[f"p_unresolved_{tag}"] = float((st == S.ST_OPEN).sum() / n)
            dur = (res[sel, k, 2] - res[sel, k, 1]) / MIN_PER_DAY
            passed = st == S.ST_PASS
            if passed.any():
                m[f"median_days_{tag}"] = float(np.median(dur[passed]))
                m[f"p90_days_{tag}"] = float(np.percentile(dur[passed], 90))
            m[f"trades_per_{tag}"] = float(np.mean(res[sel, k, 6]))
        both_mask &= ok
    # безусловные вероятности по стартам с полным горизонтом всех стадий оценки
    nb = int(both_mask.sum())
    m["n_starts_full"] = nb
    if nb:
        s1 = res[both_mask, 0, 0]
        m["p_pass_s1"] = float((s1 == S.ST_PASS).mean())
        lo, hi = _wilson(int((s1 == S.ST_PASS).sum()), nb)
        m["p_pass_s1_ci"] = [lo, hi]
        passed_all = np.ones(nb, dtype=bool)
        for k in range(len(eval_stages)):
            passed_all &= res[both_mask, k, 0] == S.ST_PASS
        m["p_pass_both"] = float(passed_all.mean())
        m["p_pass_both_ci"] = list(_wilson(int(passed_all.sum()), nb))
        if len(eval_stages) > 1:
            m["p_pass_s2"] = float((res[both_mask, 1, 0] == S.ST_PASS).mean())
        failed_any = np.zeros(nb, dtype=bool)
        daily_any = np.zeros(nb, dtype=bool)
        total_any = np.zeros(nb, dtype=bool)
        for k in range(len(eval_stages)):
            f = res[both_mask, k, 0] == S.ST_FAIL
            failed_any |= f
            daily_any |= f & (res[both_mask, k, 3] == S.R_DAILY)
            total_any |= f & (res[both_mask, k, 3] == S.R_TOTAL)
        m["p_fail_daily"] = float(daily_any.mean())
        m["p_fail_total"] = float(total_any.mean())
        m["p_fail"] = float(failed_any.mean())
        m["p_unresolved"] = float(1 - passed_all.mean() - failed_any.mean())
    if "trader" in r.stages:
        kt = r.stages.index("trader")
        funded = res[:, kt - 1, 0] == S.ST_PASS if kt > 0 else np.ones(len(starts), dtype=bool)
        tr = res[funded, kt, :]
        if len(tr):
            m["trader_n"] = int(len(tr))
            m["trader_mean_payouts_usd"] = float(np.nanmean(tr[:, 7]))
            m["trader_median_payouts_usd"] = float(np.nanmedian(tr[:, 7]))
            m["trader_p_fail"] = float((tr[:, 0] == S.ST_FAIL).mean())
            months = r.horizons_days[kt] / 30.4
            m["trader_median_monthly_usd"] = float(np.nanmedian(tr[:, 7]) / months)
            if "p_pass_both" in m:
                ev = m["p_pass_both"] * m["trader_mean_payouts_usd"]
                if r.challenge_fee_usd:
                    ev -= r.challenge_fee_usd
                m["ev_usd"] = float(ev)
    return m


def evaluate_independent(arr: SimArrays, cfg: ChallengeConfig, *, challenge_type: str = "two_stage",
                         size: float = 10_000, risk: RiskParams | None = None, stress: Stress | None = None,
                         horizon_days: float = 90, data_start_min: int, data_end_min: int,
                         step_days: int = 1) -> dict:
    """Каждая стадия оценивается отдельно с каждого старта (для коротких окон train, где цепочка
    stage1→stage2 не помещается в данные). P(both) ≈ Π P(stage k) — приближение для ранжирования."""
    risk = risk or RiskParams()
    st = stress or Stress()
    mk = cfg.market("crypto_tradfi")
    starts = rolling_starts(data_start_min, data_end_min, horizon_days, boundary_min=mk.day_boundary_min,
                            window_min=mk.reset_window_minutes, step_days=step_days)
    out: dict[str, Any] = {"n_starts": int(len(starts))}
    if len(starts) == 0:
        return out
    if st.skip_frac > 0:
        rng = np.random.default_rng(st.seed)
        arr = arr.with_skip(rng.random(arr.n) < st.skip_frac)
    hz = np.array([int(horizon_days * MIN_PER_DAY)], dtype=np.int64)
    prod = 1.0
    worst_daily = 0.0
    for k, name in enumerate(cfg.stage_names(challenge_type)):
        P = stage_params(cfg, challenge_type, name, size, risk, st)[None, :]
        res = run_chain(starts, hz, P, 1, -1.0, *arr.args())
        s = res[:, 0, 0]
        p = float((s == S.ST_PASS).mean())
        fd = float(((s == S.ST_FAIL) & (res[:, 0, 3] == S.R_DAILY)).mean())
        out[f"p_pass_s{k + 1}"] = p
        out[f"p_fail_daily_s{k + 1}"] = fd
        out[f"p_fail_s{k + 1}"] = float((s == S.ST_FAIL).mean())
        out[f"p_unresolved_s{k + 1}"] = float((s == S.ST_OPEN).mean())
        prod *= p
        worst_daily = max(worst_daily, fd)
    out["p_pass_both_approx"] = prod
    out["p_fail_daily_max"] = worst_daily
    return out
