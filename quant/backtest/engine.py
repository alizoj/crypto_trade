"""Бэктест целиком: стратегия × данные → сделки (ступень A) → челлендж (ступень B) → метрики и отчёт.

Индикаторы, стратегии и модель исполнения — тот же код, что в live (раздел 4).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl

from quant import instruments as I
from quant.backtest import sim as S
from quant.backtest.challenge import STRESS_PRESETS, ChallengeResult, RiskParams, Stress, evaluate, rolling_starts
from quant.backtest.metrics import by_group, classic_metrics
from quant.backtest.sim import trace_simulate
from quant.backtest.tradeset import TradeSet, merge, to_sim_arrays
from quant.config import ChallengeConfig, get_settings, load_challenge_config, load_strategy_config
from quant.data import store
from quant.data.aggregate import resample
from quant.montecarlo.bootstrap import bootstrap_ci
from quant.optimize import registry
from quant.research.barriers import ExecParams, execute_signals
from quant.strategies import get_strategy, load_params, with_overrides
from quant.timeutil import MIN_PER_DAY, from_min, to_min

log = logging.getLogger(__name__)
WARMUP_DAYS = 90          # прогрев индикаторов до начала периода (EMA200 на 4H ≈ 34 дня; RMA ×25)


@dataclass
class BuildSpec:
    strategy: str
    symbols: list[str]
    start: datetime
    end: datetime
    overrides: dict[str, Any] = field(default_factory=dict)        # dotted path → значение
    exec_overrides: dict[str, Any] = field(default_factory=dict)
    record_path: bool = True
    use_book_depth: bool = True


class BoundedBarsCache(dict):
    """Кэш баров с потолком по памяти: сверх бюджета бары не кэшируются и грузятся заново.

    Весь пул (172 символа с 2022 г.) в 1m с mark — ~32 ГБ, а загрузка символа с 4 годами — ~0.6 с.
    """

    def __init__(self, budget_bytes: float):
        super().__init__()
        self.budget, self.used = budget_bytes, 0

    def __setitem__(self, key, bars: pl.DataFrame) -> None:
        size = bars.estimated_size()
        if self.used + size <= self.budget:
            super().__setitem__(key, bars)
            self.used += size


def _btc_trend(strategy: str, start: datetime, end: datetime) -> pl.DataFrame | None:
    try:
        bars = store.load_bars("BTCUSDT", start, end, with_mark=False)
    except Exception:
        return None
    if bars.is_empty():
        return None
    p = load_params(strategy, "crypto_major")
    strat = get_strategy(strategy, p)
    tf = p.get("tf", "1h")
    frame = strat.compute(resample(bars, tf), hh_symbol="BTCUSDT", tf=tf, bars_1m=bars, meta={})
    if frame.is_empty() or "htf_trend" not in frame.columns:
        return None
    return frame.select("close_ts", "htf_trend")


def symbol_signals(strategy: str, inst: I.Instrument, bars: pl.DataFrame, *, params: dict, start: datetime,
                   end: datetime, btc: pl.DataFrame | None = None,
                   breadth: pl.DataFrame | None = None) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Кадр оценок и сигналы одного инструмента (сигналы только внутри [start, end)).

    breadth — ряд широты рынка (quant.backtest.breadth) для стратегий с фильтром широты.
    """
    strat = get_strategy(strategy, params)
    tf = params.get("tf", "1h")
    meta = {"instrument": inst,
            "funding": store.load_funding(inst.hh_symbol, start - timedelta(days=WARMUP_DAYS), end),
            "metrics": store.load_metrics(inst.hh_symbol, start - timedelta(days=WARMUP_DAYS), end)}
    if btc is not None and inst.group != "crypto_major":
        meta["btc_trend"] = btc
    if breadth is not None:
        meta["breadth"] = breadth
    frame = strat.compute(resample(bars, tf), hh_symbol=inst.hh_symbol, tf=tf, bars_1m=bars, meta=meta)
    sigs = strat.signals_from(frame, hh_symbol=inst.hh_symbol, tf=tf) if not frame.is_empty() else frame
    if not sigs.is_empty():
        sigs = sigs.filter((pl.col("ts") >= start) & (pl.col("ts") < end))
    return frame, sigs


def build_tradeset(spec: BuildSpec, *, cfg: ChallengeConfig | None = None,
                   bars_cache: dict[str, pl.DataFrame] | None = None) -> TradeSet:
    cfg = cfg or load_challenge_config()
    scfg = load_strategy_config(spec.strategy)
    mk = cfg.market("crypto_tradfi")
    exec_p = ExecParams.from_config(scfg.get("execution"), **spec.exec_overrides)
    insts = [I.get(s) for s in spec.symbols]
    base_params = with_overrides(load_params(spec.strategy, None), spec.overrides)
    if base_params.get("be_after_tp1") is not None and "be_after_tp1" not in spec.exec_overrides:
        exec_p.be_after_tp1 = bool(base_params["be_after_tp1"])      # стратегия задаёт безубыток после TP1 сама
    use_breadth = float(base_params.get("breadth_min_frac") or 0.0) > 0
    need_btc = spec.strategy != "baseline_random" and (use_breadth or any(i.group != "crypto_major" for i in insts))
    btc = _btc_trend(spec.strategy, spec.start - timedelta(days=WARMUP_DAYS), spec.end) if need_btc else None

    def load(key: str) -> pl.DataFrame:
        if bars_cache is not None and key in bars_cache:
            return bars_cache[key]
        b = store.load_bars(key, spec.start - timedelta(days=WARMUP_DAYS), spec.end, with_mark=True)
        if bars_cache is not None:
            bars_cache[key] = b
        return b

    breadth = None
    if use_breadth:        # доля рынка по всей вселенной instruments.csv, а не только по торгуемым символам
        from quant.backtest.breadth import cached_breadth
        from quant.strategies.feature_rules import breadth_rules

        breadth = cached_breadth(spec.start, spec.end, tf=base_params.get("tf", "1h"), htf_tf=base_params.get("htf", "4h"),
                                 rules_long=breadth_rules(base_params, "long"),
                                 rules_short=breadth_rules(base_params, "short"),
                                 load_bars=lambda inst: load(inst.hh_symbol), warmup_days=WARMUP_DAYS, btc=btc)
    parts = []
    data_start, data_end = None, None
    for inst in insts:
        key = inst.hh_symbol
        bars = load(key)
        if bars.is_empty():
            log.warning("%s: нет данных за период — пропуск", key)
            continue
        params = with_overrides(load_params(spec.strategy, inst.group), spec.overrides)
        _, sigs = symbol_signals(spec.strategy, inst, bars, params=params, start=spec.start, end=spec.end, btc=btc,
                                 breadth=breadth)
        if sigs.is_empty():
            continue
        funding = store.load_funding(key, spec.start, spec.end + timedelta(days=3))
        bd = store.load_book_depth(key, spec.start, spec.end) if spec.use_book_depth else None
        res = execute_signals(bars, sigs, exec_p, hh_symbol=key, funding=funding,
                              book_depth=bd if bd is not None and not bd.is_empty() else None,
                              record_path=spec.record_path)
        parts.append(res)
        b0 = max(to_min(spec.start), int(bars["ts"].dt.epoch("ms").min() // 60_000))
        b1 = int(bars["ts"].dt.epoch("ms").max() // 60_000)
        data_start = b0 if data_start is None else min(data_start, b0)
        data_end = b1 if data_end is None else max(data_end, b1)
        log.info("%s: сигналов %d", key, sigs.height)
    limits = {i.hh_symbol: (i.max_per_lot_usd, i.max_total_usd, i.group) for i in insts}
    meta = {"strategy": spec.strategy, "symbols": spec.symbols, "start": spec.start.isoformat(),
            "end": spec.end.isoformat(), "overrides": spec.overrides, "exec": exec_p.__dict__,
            "data_start_min": data_start or to_min(spec.start), "data_end_min": data_end or to_min(spec.end)}
    if not parts:
        raise ValueError("нет сигналов ни по одному инструменту — проверьте данные и период")
    return merge(parts, limits=limits, maker=mk.fees.maker, taker=mk.fees.taker, meta=meta)


@dataclass
class BacktestOutput:
    run_id: int
    tradeset: TradeSet
    challenge: ChallengeResult
    classic: dict
    stress: dict
    bootstrap: dict
    baseline: dict | None
    report_path: Path | None
    horizons: dict = field(default_factory=dict)


def run_backtest(spec: BuildSpec, *, challenge_type: str = "two_stage", size: float = 10_000,
                 horizon_days: float = 90, trader_horizon_days: float = 180, risk_overrides: dict | None = None,
                 n_boot: int = 100, with_baseline: bool = True, stress: bool = True, report: bool = True,
                 kind: str = "backtest", challenge_fee_usd: float | None = None,
                 extra_horizons: tuple[float, ...] = (180.0, 365.0)) -> BacktestOutput:
    """extra_horizons — дополнительные горизонты на стадию: у HashHedge срока нет («no time limit on any stage»),
    а основной H (по умолчанию 90 дней, раздел 7 ТЗ) — только окно оценки; длинные горизонты показывают, сколько
    «не завершённых» за H стартов в итоге проходят или срываются."""
    cfg = load_challenge_config()
    scfg = load_strategy_config(spec.strategy)
    risk = RiskParams.from_config(scfg.get("risk"), **(risk_overrides or {}))
    # бары переиспользуются стрессом (delay ×2) и baseline; весь пул за годы в 1m — десятки ГБ, поэтому с потолком
    cache = BoundedBarsCache(4e9)
    ts = build_tradeset(spec, cfg=cfg, bars_cache=cache)
    arr = to_sim_arrays(ts)
    d0, d1 = ts.data_start_min, ts.data_end_min
    fee = challenge_fee_usd if challenge_fee_usd is not None else cfg.challenge_fee_usd.get(challenge_type, {}).get(int(size))
    ch = evaluate(arr, cfg, challenge_type=challenge_type, size=size, risk=risk, horizon_days=horizon_days,
                  trader_horizon_days=trader_horizon_days, data_start_min=d0, data_end_min=d1,
                  challenge_fee_usd=fee)
    classic = classic_metrics(ts.trades)
    horizons = _horizon_table(arr, cfg, challenge_type=challenge_type, size=size, risk=risk, base_h=horizon_days,
                              extra=extra_horizons, d0=d0, d1=d1)
    stress_tab = {}
    if stress:
        for name, st in STRESS_PRESETS.items():
            if name == "base":
                continue
            stress_tab[name] = evaluate(arr, cfg, challenge_type=challenge_type, size=size, risk=risk, stress=st,
                                        horizon_days=horizon_days, data_start_min=d0, data_end_min=d1,
                                        include_trader=False).metrics
        # задержка ручного исполнения ×2 требует пересчёта ступени A
        spec2 = BuildSpec(**{**spec.__dict__, "exec_overrides": {**spec.exec_overrides, "delay_mult": 2.0}})
        ts2 = build_tradeset(spec2, cfg=cfg, bars_cache=cache)
        stress_tab["delay_x2"] = evaluate(to_sim_arrays(ts2), cfg, challenge_type=challenge_type, size=size, risk=risk,
                                          horizon_days=horizon_days, data_start_min=d0, data_end_min=d1,
                                          include_trader=False).metrics
    boot = bootstrap_ci(arr, cfg, data_start_min=d0, data_end_min=d1, n_boot=n_boot, risk=risk,
                        horizon_days=horizon_days, challenge_type=challenge_type, size=size) if n_boot else {}
    base_metrics = None
    if with_baseline and spec.strategy != "baseline_random":
        bspec = BuildSpec(strategy="baseline_random", symbols=spec.symbols, start=spec.start, end=spec.end,
                          use_book_depth=spec.use_book_depth)
        bts = build_tradeset(bspec, cfg=cfg, bars_cache=cache)
        bres = evaluate(to_sim_arrays(bts), cfg, challenge_type=challenge_type, size=size, risk=risk,
                        horizon_days=horizon_days, data_start_min=bts.data_start_min, data_end_min=bts.data_end_min,
                        include_trader=False)
        base_metrics = {**bres.metrics, **{f"classic_{k}": v for k, v in classic_metrics(bts.trades).items()}}
        bboot = bootstrap_ci(to_sim_arrays(bts), cfg, data_start_min=bts.data_start_min,
                             data_end_min=bts.data_end_min, n_boot=max(20, n_boot // 2), risk=risk,
                             horizon_days=horizon_days, challenge_type=challenge_type, size=size) if n_boot else {}
        base_metrics["bootstrap"] = bboot
    dhash = store.dataset_hash(spec.symbols, spec.start - timedelta(days=WARMUP_DAYS), spec.end)
    params = {"strategy_params": {s: with_overrides(load_params(spec.strategy, I.get(s).group), spec.overrides)
                                  for s in spec.symbols[:1]}, "overrides": spec.overrides}
    metrics = {**ch.metrics, "classic": classic, "stress": stress_tab, "bootstrap": boot,
               "baseline": base_metrics, "horizons": horizons}
    run_id = registry.record_run(kind=kind, strategy=spec.strategy, params=params, dataset_hash=dhash,
                                 config={"challenge_type": challenge_type, "size": size, "horizon_days": horizon_days,
                                         "risk": risk.__dict__, "options": cfg.options.model_dump(),
                                         "symbols": spec.symbols, "start": spec.start.isoformat(),
                                         "end": spec.end.isoformat()},
                                 metrics=_flat_metrics(metrics))
    out_dir = get_settings().reports_dir / f"run_{run_id:05d}"
    ts.save(out_dir / "trades")
    report_path = None
    if report:
        from quant.reports.backtest_report import build_report

        report_path = build_report(run_id=run_id, spec=spec, tradeset=ts, challenge=ch, classic=classic,
                                   stress=stress_tab, bootstrap=boot, baseline=base_metrics, risk=risk,
                                   cfg=cfg, challenge_type=challenge_type, size=size, dataset_hash=dhash,
                                   out_path=out_dir / "report.html", horizons=horizons)
    registry.update_run(run_id, report_path=str(report_path) if report_path else None, artifacts_dir=str(out_dir))
    return BacktestOutput(run_id=run_id, tradeset=ts, challenge=ch, classic=classic, stress=stress_tab,
                          bootstrap=boot, baseline=base_metrics, report_path=report_path, horizons=horizons)


def _horizon_table(arr, cfg: ChallengeConfig, *, challenge_type: str, size: float, risk: RiskParams, base_h: float,
                   extra: tuple[float, ...], d0: int, d1: int) -> dict:
    """P(pass) на нескольких горизонтах на ОДНОМ наборе стартов — тех, у кого данных хватает на самый длинный
    (иначе у каждого горизонта свои старты, и числа несравнимы: P растёт с H только на одних и тех же стартах)."""
    hs = sorted({float(base_h), *(float(x) for x in extra if float(x) > base_h)})
    if len(hs) < 2:
        return {}
    mk = cfg.market("crypto_tradfi")
    n_stages = len(cfg.stage_names(challenge_type))
    common = rolling_starts(d0, d1, n_stages * hs[-1], boundary_min=mk.day_boundary_min,
                            window_min=mk.reset_window_minutes)
    if not len(common):
        return {"_note": f"нет стартов с {n_stages * hs[-1]:g} днями данных впереди"}
    out: dict = {"_starts": [from_min(int(common[0])).isoformat(), from_min(int(common[-1])).isoformat()]}
    for h in hs:
        hm = evaluate(arr, cfg, challenge_type=challenge_type, size=size, risk=risk, horizon_days=h,
                      data_start_min=d0, data_end_min=d1, include_trader=False, starts=common).metrics
        out[f"{h:g}"] = {k: hm.get(k) for k in ("n_starts_full", "p_pass_s1", "p_pass_both", "p_fail_daily",
                                                "p_fail_total", "p_unresolved", "median_days_s1", "median_days_s2")}
    return out


def _flat_metrics(m: dict) -> dict:
    """Метрики для списка прогонов (экран 5): ключевые поля наверху + полные данные внутри."""
    c = m.get("classic", {})
    b = m.get("bootstrap", {})
    flat = {
        "p_pass_s1": m.get("p_pass_s1"), "p_pass_s2": m.get("p_pass_s2"), "p_pass_both": m.get("p_pass_both"),
        "p_fail_daily": m.get("p_fail_daily"), "p_fail_total": m.get("p_fail_total"),
        "p_unresolved": m.get("p_unresolved"), "median_days_s1": m.get("median_days_s1"),
        "p90_days_s1": m.get("p90_days_s1"), "ev_usd": m.get("ev_usd"), "pf": c.get("pf"),
        "winrate": c.get("winrate"), "avg_r": c.get("avg_r"), "n_trades": c.get("n_trades"),
        "p_pass_both_ci": b.get("p_pass_both"), "full": m,
    }
    return flat


def sample_equity_curves(arr, cfg: ChallengeConfig, ch: ChallengeResult, *, challenge_type: str, size: float,
                         risk: RiskParams, horizon_days: float, n: int = 30, seed: int = 0) -> list[dict]:
    """Кривые баланса стадии 1 для выборки стартов (по журналу симулятора)."""
    from quant.backtest.challenge import stage_params

    if len(ch.starts) == 0:
        return []
    rng = np.random.default_rng(seed)
    idx = sorted(rng.choice(len(ch.starts), size=min(n, len(ch.starts)), replace=False))
    P = stage_params(cfg, challenge_type, cfg.stage_names(challenge_type)[0], size, risk, Stress())
    out = []
    for i in idx:
        s = int(ch.starts[i])
        (status, end_m, reason, *_), trace = trace_simulate(P, s, int(horizon_days * MIN_PER_DAY), arr)
        xs = [0.0] + [(r[0] - s) / MIN_PER_DAY for r in trace if r[1] in (1, 2, 5, 9)] + [(end_m - s) / MIN_PER_DAY]
        bal = [size] + [r[4] for r in trace if r[1] in (1, 2, 5, 9)]
        bal.append(bal[-1])
        out.append({"x": xs, "y": bal, "status": int(status), "reason": int(reason), "start": from_min(s)})
    return out
