"""CLI: quant optimize walkforward | holdout (фаза 5)."""

from __future__ import annotations

import argparse
import json
import logging
from datetime import timedelta

from quant.backtest.cli import symbols_arg
from quant.config import get_settings, load_challenge_config, load_strategy_config
from quant.timeutil import from_min, now_utc, parse_dt, to_min

log = logging.getLogger(__name__)


def flatten(d: dict, prefix: str = "") -> dict:
    out = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(flatten(v, key))
        else:
            out[key] = v
    return out


def _walkforward(args: argparse.Namespace) -> int:
    from quant.backtest.challenge import RiskParams
    from quant.backtest.engine import WARMUP_DAYS, BoundedBarsCache, BuildSpec, build_tradeset
    from quant.data import store
    from quant.optimize import registry
    from quant.optimize.walkforward import WFConfig, run_walkforward
    from quant.reports.optimize_report import build_wf_report

    symbols = symbols_arg(args)
    start = parse_dt(args.start)
    end = parse_dt(args.end) if args.end else now_utc()
    scfg = load_strategy_config(args.strategy)
    space = json.loads(args.space) if args.space else scfg.get("search_space", {})
    risk = RiskParams.from_config(scfg.get("risk"))
    cfg = load_challenge_config()
    cache = BoundedBarsCache(args.bars_cache_gb * 1e9)

    def make(params: dict):
        return build_tradeset(BuildSpec(strategy=args.strategy, symbols=symbols, start=start, end=end,
                                        overrides=flatten(params)), cfg=cfg, bars_cache=cache)

    dhash = store.dataset_hash(symbols, start - timedelta(days=WARMUP_DAYS), end)
    wf = WFConfig(challenge_type=args.challenge, size=args.size, horizon_days=args.horizon,
                  fail_daily_max=args.fail_daily_max, max_combos=args.max_combos, holdout_months=args.holdout_months,
                  train_months=args.train_months, test_months=args.test_months, step_months=args.step_months,
                  step_days=args.step_days)
    hold = int(scfg.get("params", {}).get("max_hold_min", 1440))
    study = args.study or f"wf:{args.strategy}:{args.group_label or ','.join(symbols[:3])}"
    res = run_walkforward(study=study, strategy=args.strategy, make_tradeset=make, base_params={},
                          search_space=space, chall_cfg=cfg, risk=risk, wf=wf, data_start=to_min(start),
                          data_end=to_min(end), dataset_hash=dhash, embargo_min=max(hold, 1440),
                          group=args.group_label or "")
    final = res.combos[res.final_combo]["point"] if res.final_combo >= 0 else {}
    metrics = {"oos": res.oos_metrics, "oos_classic": res.oos_classic, "dsr": res.dsr, "pbo": res.pbo,
               "final_params": final, "holdout_start": from_min(res.holdout_start).isoformat(),
               "n_trials": registry.count_trials(study),
               "p_pass_s1": res.oos_metrics.get("p_pass_s1"), "p_pass_both": res.oos_metrics.get("p_pass_both"),
               "p_fail_daily": res.oos_metrics.get("p_fail_daily"), "p_unresolved": res.oos_metrics.get("p_unresolved"),
               "dsr_value": res.dsr.get("dsr"), "pbo_value": res.pbo.get("pbo"),
               "winrate": res.oos_classic.get("winrate"), "avg_r": res.oos_classic.get("avg_r"),
               "n_trades": res.oos_classic.get("n_trades")}
    run_id = registry.record_run(kind="walkforward", strategy=args.strategy, params={"final": final, "study": study},
                                 dataset_hash=dhash, config={"symbols": symbols, "wf": wf.__dict__,
                                                             "start": start.isoformat(), "end": end.isoformat()},
                                 metrics=metrics)
    out_dir = get_settings().reports_dir / f"run_{run_id:05d}"
    out_dir.mkdir(parents=True, exist_ok=True)
    if res.oos_trades is not None:
        res.oos_trades.save(out_dir / "oos")
    report = build_wf_report(run_id=run_id, strategy=args.strategy, symbols=symbols, res=res, wf=wf,
                             out_path=out_dir / "report.html")
    registry.update_run(run_id, report_path=str(report), artifacts_dir=str(out_dir))
    print(f"walk-forward #{run_id}: окон {len(res.windows)}, вариантов {len(res.combos)}; финальные параметры {final}")
    print(f"OOS: P(s1)={res.oos_metrics.get('p_pass_s1')} P(both)={res.oos_metrics.get('p_pass_both')} "
          f"DSR={res.dsr.get('dsr')} PBO={res.pbo.get('pbo')}")
    print(f"hold-out не тронут, начинается {from_min(res.holdout_start):%Y-%m-%d}. Отчёт: {report}")
    return 0


def _holdout(args: argparse.Namespace) -> int:
    from sqlalchemy import select

    from quant.backtest.engine import BuildSpec, run_backtest
    from quant.db import session_scope
    from quant.db.models import BacktestRun
    from quant.optimize import registry

    with session_scope() as s:
        run = s.get(BacktestRun, args.wf_run)
        if run is None or run.kind != "walkforward":
            raise SystemExit(f"нет walk-forward прогона #{args.wf_run}")
        final = run.params.get("final", {})
        conf = run.config
        hold_start = parse_dt(run.metrics["holdout_start"])
    params_key = {"final": final, "wf_run": args.wf_run}
    if registry.holdout_used(run.strategy, params_key) and not args.force:
        raise SystemExit("hold-out для этих параметров уже использовался. Повтор обесценивает проверку "
                         "(--force, если вы осознанно идёте на это)")
    spec = BuildSpec(strategy=run.strategy, symbols=conf["symbols"], start=hold_start, end=parse_dt(conf["end"]),
                     overrides=final)
    out = run_backtest(spec, challenge_type=conf["wf"]["challenge_type"], size=conf["wf"]["size"],
                       horizon_days=min(conf["wf"]["horizon_days"], 60), n_boot=args.boot, kind="holdout")
    registry.update_run(out.run_id, params=params_key)
    m = out.challenge.metrics
    print(f"hold-out #{out.run_id}: P(s1)={m.get('p_pass_s1')} P(both)={m.get('p_pass_both')} "
          f"сделок {out.classic.get('n_trades')}. Отчёт: {out.report_path}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("optimize", help="walk-forward оптимизация и hold-out (фаза 5)")
    s = p.add_subparsers(dest="cmd", required=True)
    w = s.add_parser("walkforward", help="окна train/test с purge/embargo, плато, OOS, DSR/PBO")
    w.add_argument("--strategy", default="confluence_v1")
    w.add_argument("--symbols")
    w.add_argument("--groups")
    w.add_argument("--group-label", help="имя пула (crypto_major/crypto_alt/tradfi) для реестра")
    w.add_argument("--start", required=True)
    w.add_argument("--end")
    w.add_argument("--space", help="JSON search_space")
    w.add_argument("--max-combos", type=int, default=24)
    w.add_argument("--challenge", default="two_stage")
    w.add_argument("--size", type=float, default=10_000)
    w.add_argument("--horizon", type=float, default=90)
    w.add_argument("--fail-daily-max", type=float, default=0.35)
    w.add_argument("--holdout-months", type=float, default=6)
    w.add_argument("--train-months", type=float, default=6)
    w.add_argument("--test-months", type=float, default=2)
    w.add_argument("--step-months", type=float, default=2)
    w.add_argument("--step-days", type=int, default=2, help="шаг rolling start в днях (ускорение)")
    w.add_argument("--study")
    w.add_argument("--bars-cache-gb", type=float, default=4,
                   help="потолок кэша 1m-баров между вариантами; остальное перечитывается с диска")
    w.set_defaults(func=_walkforward)
    h = s.add_parser("holdout", help="однократная проверка финальных параметров на hold-out")
    h.add_argument("--wf-run", type=int, required=True)
    h.add_argument("--boot", type=int, default=100)
    h.add_argument("--force", action="store_true")
    h.set_defaults(func=_holdout)
