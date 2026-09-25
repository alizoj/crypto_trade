"""Санити-чек модели (приёмка фазы 4).

1. stage_b_fixed_risk: честная монета, фиксированный риск 0.5%/сделку, R:R 1:1, нулевые издержки,
   неограниченный горизонт → P(pass stage1) = 10/(10+8) = 0.556 (задача о разорении) ± 3σ MC.
2. stage_b_governor: risk governor по умолчанию (10% оставшегося запаса), горизонт 90 дней →
   доля fail ≈ 0 при большой доле unresolved (счёт «зависает» у floor, раздел 5).
3. stage_a_fair: мартингал 1m + симметричные барьеры → доля TP ≈ 0.5 (исполнение не смещено).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import polars as pl

from quant.backtest import sim as S
from quant.backtest.challenge import RiskParams, Stress, stage_params
from quant.backtest.sim import run_chain
from quant.backtest.synthetic import T0, fair_trades, random_walk_bars
from quant.config import ChallengeConfig, load_challenge_config
from quant.research.barriers import FILLED, ExecParams, execute_signals
from quant.schemas import SIGNALS, conform


@dataclass
class SanityResult:
    name: str
    value: float
    expected: float
    tolerance: float
    n: int
    extra: dict

    @property
    def ok(self) -> bool:
        return abs(self.value - self.expected) <= self.tolerance

    def line(self) -> str:
        flag = "OK " if self.ok else "FAIL"
        return (f"[{flag}] {self.name}: {self.value:.4f} (ожидание {self.expected:.4f} ± {self.tolerance:.4f}, n={self.n})"
                + (f" {self.extra}" if self.extra else ""))


def _zero_cost_cfg(cfg: ChallengeConfig) -> ChallengeConfig:
    mk = cfg.markets["crypto_tradfi"].model_copy(update={
        "fees": cfg.markets["crypto_tradfi"].fees.model_copy(update={"maker": 0.0, "taker": 0.0})})
    return cfg.model_copy(update={"markets": {**cfg.markets, "crypto_tradfi": mk}})


def stage_b_fixed_risk(n_challenges: int = 20_000, *, batch: int = 500, per_challenge: int = 2_000,
                       seed: int = 1, cfg: ChallengeConfig | None = None) -> SanityResult:
    cfg = _zero_cost_cfg(cfg or load_challenge_config())
    risk = RiskParams(fixed_risk_pct=0.005, soft_daily_stop=0.0, max_concurrent_positions=1,
                      max_correlated_exposure_pct=0.0, target_lock=False, min_notional_usd=0.0)
    P = stage_params(cfg, "two_stage", "stage1", 10_000, risk, Stress())[None, :]
    P[0, S.P_LIQ] = 0.0
    statuses = []
    unresolved = 0
    for b in range(math.ceil(n_challenges / batch)):
        nb = min(batch, n_challenges - b * batch)
        arr, starts, seg = fair_trades(nb, per_challenge, seed=seed * 100_003 + b)
        res = run_chain(starts, np.array([seg - 1], dtype=np.int64), P, 1, -1.0, *arr.args())
        st = res[:, 0, 0]
        statuses.append(st)
        unresolved += int((st == S.ST_OPEN).sum())
    st = np.concatenate(statuses)
    p = float((st == S.ST_PASS).mean())
    theory = 10 / 18
    se = math.sqrt(theory * (1 - theory) / len(st))
    return SanityResult("stage_b_fixed_risk: P(pass stage1)", p, theory, 3 * se, len(st),
                        {"unresolved": unresolved})


def stage_b_governor(n_challenges: int = 2_000, *, horizon_days: int = 90, seed: int = 2,
                     cfg: ChallengeConfig | None = None) -> SanityResult:
    cfg = _zero_cost_cfg(cfg or load_challenge_config())
    risk = RiskParams(max_concurrent_positions=1, soft_daily_stop=0.0, max_correlated_exposure_pct=0.0,
                      target_lock=False, min_notional_usd=0.0)
    P = stage_params(cfg, "two_stage", "stage1", 10_000, risk, Stress())[None, :]
    arr, starts, _ = fair_trades(n_challenges, int(horizon_days * 1440 / 160) + 5, seed=seed)
    res = run_chain(starts, np.array([horizon_days * 1440], dtype=np.int64), P, 1, -1.0, *arr.args())
    st = res[:, 0, 0]
    p_fail = float((st == S.ST_FAIL).mean())
    return SanityResult("stage_b_governor: P(fail) за 90 дней", p_fail, 0.0, 0.01, len(st),
                        {"p_pass": round(float((st == S.ST_PASS).mean()), 3),
                         "p_unresolved": round(float((st == S.ST_OPEN).mean()), 3)})


def stage_a_fair(n_min: int = 300_000, seed: int = 3) -> SanityResult:
    bars = random_walk_bars(n_min, seed=seed)
    closes = bars["close"].to_numpy()
    rows = []
    for idx, i in enumerate(range(100, n_min - 1_000, 45)):
        p = float(closes[i - 1])
        side = 1 if idx % 2 == 0 else -1
        rows.append({"ts": i, "side": side, "entry_price": p, "sl": p * (1 - side * 0.004),
                     "tp1": p * (1 + side * 0.004)})
    t0_ms = int(T0.timestamp() * 1000)
    sig = pl.DataFrame(rows).with_columns(
        (pl.col("ts") * 60_000 + t0_ms).cast(pl.Datetime("ms", "UTC")).alias("ts"),
        pl.lit("SYN").alias("hh_symbol"), pl.lit("1h").alias("tf"), pl.lit("market").alias("entry_type"),
        pl.lit(None, dtype=pl.Float64).alias("tp2"), pl.lit(1.0).alias("tp1_frac"), pl.lit(50.0).alias("score"),
        pl.lit(100_000).alias("max_hold_min"), pl.lit(60).alias("valid_min"))
    sig = conform(sig, SIGNALS)
    t = execute_signals(bars, sig, ExecParams(exec_delay_min=(0, 0), slippage_k_atr=0.0), hh_symbol="SYN",
                        record_path=False).trades
    lab = t.filter((pl.col("status") == FILLED) & (pl.col("label") >= 0))["label"].to_numpy()
    p = float(np.mean(lab == 1))
    se = math.sqrt(0.25 / len(lab))
    amb = float(t.filter(pl.col("status") == FILLED)["ambiguous"].mean())
    return SanityResult("stage_a_fair: доля TP на мартингале", p, 0.5, 3 * se + 0.005, len(lab),
                        {"ambiguous_share": round(amb, 4)})


def run_all(quick: bool = False) -> list[SanityResult]:
    if quick:
        return [stage_b_fixed_risk(4_000), stage_b_governor(500), stage_a_fair(150_000)]
    return [stage_b_fixed_risk(), stage_b_governor(), stage_a_fair()]
