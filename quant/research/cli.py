"""CLI: quant research screen | exits."""

from __future__ import annotations

import argparse
import json

import polars as pl

from quant.backtest.cli import symbols_arg
from quant.config import load_strategy_config
from quant.timeutil import now_utc, parse_dt


def _screen(args: argparse.Namespace) -> int:
    from quant.research.screen import screen

    space = json.loads(args.space) if args.space else load_strategy_config(args.strategy).get("search_space", {})
    df = screen(args.strategy, symbols_arg(args), parse_dt(args.start), parse_dt(args.end) if args.end else now_utc(),
                search_space=space, study=args.study or f"screen:{args.strategy}", max_combos=args.max_combos,
                bars_cache_gb=args.bars_cache_gb)
    with pl.Config(tbl_rows=40, tbl_cols=20, tbl_width_chars=200):
        print(df.head(args.top))
    if args.out:
        df.write_csv(args.out)
    return 0


def _exits(args: argparse.Namespace) -> int:
    from quant.backtest.engine import BuildSpec, build_tradeset
    from quant.research.screen import exit_study

    overrides = json.loads(args.overrides) if args.overrides else {}
    ts = build_tradeset(BuildSpec(strategy=args.strategy, symbols=symbols_arg(args), start=parse_dt(args.start),
                                  end=parse_dt(args.end) if args.end else now_utc(), overrides=overrides,
                                  record_path=False, use_book_depth=False))
    with pl.Config(tbl_rows=40):
        print(exit_study(ts.trades))
    return 0


def _forward(args: argparse.Namespace) -> int:
    from datetime import timedelta
    from pathlib import Path

    from quant.config import get_settings
    from quant.research.forward import evaluate_all, load_registry

    reg = load_registry(Path(args.config) if args.config else None)
    today = now_utc().replace(hour=0, minute=0, second=0, microsecond=0)
    end = parse_dt(args.end) if args.end else today
    print(f"реестр от {reg.registered}; новые данные {reg.start:%Y-%m-%d} … {end:%Y-%m-%d} "
          f"({(end - reg.start) / timedelta(days=1):.0f} дн.); вердикт — при >= {reg.min_trades} сделок сетапа на прогон")
    res = evaluate_all(reg, end, names=args.name)
    for r in res:
        s = r["setup"]
        ctrl = ", ".join("—" if x["r"] is None else f"{x['r']:+.3f} ({x['n']})" for x in r["control"])
        rs = "—" if s["r"] is None else f"{s['r']:+.3f} ± {s['se'] or 0:.3f}"
        print(f"  {r['name']:22} сетап: сделок {s['n']:5} ({s['runs']} прог.), R/сд {rs} | контроль: {ctrl} → {r['verdict']}")
    out = get_settings().reports_dir / "forward" / f"forward_{now_utc():%Y%m%d_%H%M}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"registered": reg.registered, "start": reg.start.isoformat(), "end": end.isoformat(),
                               "results": res}, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    print(f"сохранено: {out}")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("research", help="уровень 1: быстрый скрининг гипотез и выходов")
    s = p.add_subparsers(dest="cmd", required=True)
    a = s.add_parser("screen", help="сетка параметров → метки TP/SL/таймаут, MFE/MAE, метрики в R")
    for x in (a,):
        x.add_argument("--strategy", default="confluence_v1")
        x.add_argument("--symbols")
        x.add_argument("--groups")
        x.add_argument("--start", required=True)
        x.add_argument("--end")
    a.add_argument("--space", help="JSON search_space; по умолчанию из yaml стратегии")
    a.add_argument("--max-combos", type=int, default=64)
    a.add_argument("--study")
    a.add_argument("--top", type=int, default=20)
    a.add_argument("--bars-cache-gb", type=float, default=4,
                   help="потолок кэша 1m-баров между вариантами; остальное перечитывается с диска")
    a.add_argument("--out", help="сохранить полную таблицу вариантов в CSV")
    a.set_defaults(func=_screen)
    e = s.add_parser("exits", help="сравнение выходов по MFE/MAE без повторного прогона входов")
    e.add_argument("--strategy", default="confluence_v1")
    e.add_argument("--symbols")
    e.add_argument("--groups")
    e.add_argument("--start", required=True)
    e.add_argument("--end")
    e.add_argument("--overrides")
    e.set_defaults(func=_exits)
    f = s.add_parser("forward", help="проверка зарегистрированных кандидатов на новых данных (config/forward)")
    f.add_argument("--config", help="реестр кандидатов, по умолчанию config/forward/candidates.yaml")
    f.add_argument("--end", help="конец периода (по умолчанию — начало текущих суток UTC)")
    f.add_argument("--name", nargs="+", help="только эти кандидаты")
    f.set_defaults(func=_forward)
