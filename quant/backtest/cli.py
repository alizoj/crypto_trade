"""CLI: quant backtest ..., quant sanity."""

from __future__ import annotations

import argparse
import json
import logging

from quant import instruments as I
from quant.timeutil import now_utc, parse_dt

log = logging.getLogger(__name__)


def symbols_arg(args: argparse.Namespace) -> list[str]:
    if getattr(args, "symbols", None):
        return [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    if getattr(args, "groups", None):
        return [i.hh_symbol for i in I.select(groups=args.groups.split(","))]
    raise SystemExit("укажите --symbols или --groups")


def _run(args: argparse.Namespace) -> int:
    from quant.backtest.engine import BuildSpec, run_backtest

    overrides = json.loads(args.overrides) if args.overrides else {}
    spec = BuildSpec(strategy=args.strategy, symbols=symbols_arg(args), start=parse_dt(args.start),
                     end=parse_dt(args.end) if args.end else now_utc(), overrides=overrides)
    risk = json.loads(args.risk) if args.risk else {}
    out = run_backtest(spec, challenge_type=args.challenge, size=args.size, horizon_days=args.horizon,
                       n_boot=args.boot, with_baseline=not args.no_baseline, stress=not args.no_stress,
                       risk_overrides=risk, challenge_fee_usd=args.fee)
    m = out.challenge.metrics
    print(f"прогон #{out.run_id}: сделок {out.classic.get('n_trades')}, "
          f"P(s1)={m.get('p_pass_s1')}, P(both)={m.get('p_pass_both')}, P(fail daily)={m.get('p_fail_daily')}, "
          f"не завершено={m.get('p_unresolved')}")
    if out.report_path:
        print(f"отчёт: {out.report_path}")
    return 0


def _sanity(args: argparse.Namespace) -> int:
    from quant.backtest.sanity import run_all
    from quant.optimize import registry

    results = run_all(quick=args.quick)
    for r in results:
        print(r.line())
    registry.record_run(kind="sanity", strategy="baseline_random", params={"quick": args.quick},
                        dataset_hash="synthetic", config={}, metrics={r.name: {"value": r.value, "expected": r.expected,
                                                                                 "tolerance": r.tolerance, "ok": r.ok}
                                                                        for r in results})
    return 0 if all(r.ok for r in results) else 1


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("backtest", help="бэктест стратегии по правилам челленджа (фаза 4)")
    s = p.add_subparsers(dest="cmd", required=True)
    r = s.add_parser("run", help="сделки → rolling-start челленджи → метрики, стресс, bootstrap, отчёт")
    r.add_argument("--strategy", default="confluence_v1")
    r.add_argument("--symbols")
    r.add_argument("--groups", help="crypto_major,crypto_alt,tradfi")
    r.add_argument("--start", required=True)
    r.add_argument("--end")
    r.add_argument("--challenge", default="two_stage", choices=["two_stage", "one_stage"])
    r.add_argument("--size", type=float, default=10_000)
    r.add_argument("--horizon", type=float, default=90, help="горизонт H на стадию, дней")
    r.add_argument("--boot", type=int, default=100, help="число bootstrap-повторов (0 — без)")
    r.add_argument("--fee", type=float, help="цена челленджа для EV")
    r.add_argument("--overrides", help='JSON dotted-путей: {"long.threshold": 70}')
    r.add_argument("--risk", help='JSON risk-параметров: {"risk_per_trade": 0.1}')
    r.add_argument("--no-baseline", action="store_true")
    r.add_argument("--no-stress", action="store_true")
    r.set_defaults(func=_run)
    q = sub.add_parser("sanity", help="санити-чек модели (приёмка фазы 4)")
    q.add_argument("--quick", action="store_true")
    q.set_defaults(func=_sanity)
