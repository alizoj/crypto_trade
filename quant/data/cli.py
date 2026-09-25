"""CLI слоя данных: `quant instruments ...` и `quant data ...` (регистрируется в quant.cli)."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import timedelta
from pathlib import Path

log = logging.getLogger(__name__)


def _quiet_http() -> None:
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:  # консоль Windows (cp1251) не должна падать на символах вне кодировки
        sys.stdout.reconfigure(errors="replace")
    except Exception:  # noqa: BLE001
        pass


def _split(values: list[str] | None) -> list[str]:
    out: list[str] = []
    for v in values or []:
        out += [x.strip() for x in v.replace(";", ",").split(",") if x.strip()]
    return out


def _add_selection(p: argparse.ArgumentParser) -> None:
    p.add_argument("--symbols", nargs="+", help="hh_symbol через запятую/пробел (BTCUSDT,ETHUSDT)")
    p.add_argument("--groups", nargs="+", help="crypto_major | crypto_alt | tradfi")
    p.add_argument("--all", action="store_true", help="все инструменты из instruments.csv")


def _select(args, *, default_all: bool = False, default: list[str] | None = None):
    from quant.instruments import instruments, select

    syms, groups = _split(args.symbols), _split(getattr(args, "groups", None))
    if getattr(args, "all", False) or (not syms and not groups and default_all):
        return list(instruments())
    if not syms and not groups:
        if default is not None:
            wanted = set(default)
            return [i for i in instruments() if i.hh_symbol in wanted]
        raise SystemExit("укажите --symbols, --groups или --all")
    items = select(syms or None, groups=groups or None)
    unknown = {s.upper() for s in syms} - {i.hh_symbol for i in items} - {i.base for i in items}
    if unknown:
        raise SystemExit(f"нет в instruments.csv: {', '.join(sorted(unknown))}")
    return items


def _fmt_ts(v) -> str:
    return v.strftime("%Y-%m-%d %H:%M") if v is not None else "-"


def _table(rows: list[list], header: list[str]) -> str:
    cells = [header] + [[str(c) for c in r] for r in rows]
    w = [max(len(r[i]) for r in cells) for i in range(len(header))]
    line = lambda r: "  ".join(c.rjust(w[i]) if i else c.ljust(w[i]) for i, c in enumerate(r))  # noqa: E731
    return "\n".join([line(cells[0]), "  ".join("-" * x for x in w)] + [line(r) for r in cells[1:]])


def _dir_size(p: Path) -> int:
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.exists() else 0


# ---------------------------------------------------------------- команды

def cmd_validate(args) -> int:
    _quiet_http()
    from quant.data.validate import format_report, validate

    items = _select(args, default_all=True)
    results = validate(items, check_divergence=not args.no_divergence, workers=args.workers)
    print(format_report(results))
    return 0 if all(r.ok for r in results) else 1


def cmd_backfill(args) -> int:
    _quiet_http()
    from quant.data.backfill import backfill, parse_end
    from quant.data.datasets import resolve
    from quant.timeutil import parse_dt

    items = _select(args)
    datasets = resolve(args.datasets)
    start = parse_dt(args.start) if args.start else None
    end = parse_end(args.end)
    rep = backfill(items, datasets, start, end, workers=args.workers, keep_zip=args.keep_zip,
                   rest_tail=not args.no_rest_tail, recheck=args.recheck, clip_history=not args.no_clip_history,
                   trades_min_per_lot=args.trades_min_per_lot)
    a = rep["archive"]
    print(f"архив: файлов {a['total']}, загружено {a['downloaded']} ({a['mb']} МБ), пропущено {a['skipped']}, "
          f"ошибок {a['failed']}; конец диапазона {rep['end']}")
    for e in a["errors"]:
        print(f"  ошибка: {e}")
    for hh, r in sorted(rep["rest"].items()):
        print(f"  REST {hh}: " + ", ".join(f"{k}={v}" for k, v in r.items()))
    return 1 if a["failed"] else 0


def cmd_build(args) -> int:
    _quiet_http()
    from quant.data.build import NORM_DATASETS, build
    from quant.data.datasets import raw_dir

    items = _select(args, default=[i for i in _all_hh() if raw_dir("klines_1m", _src(i)).exists()])
    datasets = _split(args.datasets) or None
    if datasets:
        bad = set(datasets) - set(NORM_DATASETS)
        if bad:
            raise SystemExit(f"неизвестные norm-датасеты: {sorted(bad)}; есть {NORM_DATASETS}")
    res = build([i.hh_symbol for i in items], datasets, halt_min_bars=args.halt_min_bars)
    rows = []
    for hh, r in res.items():
        if "error" in r:
            rows.append([hh, "ОШИБКА: " + r["error"][:80], "", "", "", "", ""])
            continue
        g = lambda k, f="rows": r.get(k, {}).get(f, "-")  # noqa: E731
        rows.append([hh, g("bars_1m"), g("bars_1m", "halted"), g("mark_1m"), g("funding"), g("metrics"),
                     g("book_depth"), len(r.get("agg_trades", {}).get("months", []) or [])])
    print(_table(rows, ["symbol", "bars_1m", "halted", "mark_1m", "funding", "metrics", "book_depth", "agg_months"]))
    return 1 if any("error" in r for r in res.values()) else 0


def cmd_quality(args) -> int:
    _quiet_http()
    from quant.data import store
    from quant.data.quality import load_registry, run_quality

    avail = store.available_symbols()
    items = _select(args, default=avail)
    reps = run_quality([i.hh_symbol for i in items], halt_min_bars=args.halt_min_bars)
    rows = []
    fmt = lambda d: "; ".join(f"{k}:{v['count']}/{v['minutes']}m" for k, v in sorted(d.items())) or "-"  # noqa: E731
    for r in reps:
        div = f"{r['bybit_divergence_median_pct']:.3f}" if r["bybit_divergence_median_pct"] is not None else "-"
        lag = f"{r['history_lag_min'] / 1440:.0f}" if r["history_lag_min"] else "0"
        rows.append([r["hh_symbol"], r["rows"], lag, f"{r['halted_pct']:.3f}", f"{r['zero_vol_pct']:.3f}",
                     f"{r['synthetic_pct']:.1f}", r["missing_min"], r["unknown_gaps"], div, fmt(r["gaps"]),
                     fmt(r["mark_gaps"])])
    print(_table(rows, ["symbol", "rows", "lag_d", "halted%", "zero_vol%", "synth%", "missing_min", "unknown",
                        "bybit_div%", "klines: причина:кол-во/минут", "mark: причина:кол-во/минут"]))
    if args.details:
        for g in load_registry([i.hh_symbol for i in items], open_only=True):
            print(f"  {g['hh_symbol']:<12} {g['dataset']:<18} {_fmt_ts(g['start_ts'])} … {_fmt_ts(g['end_ts'])} "
                  f"{g['minutes']:>7} мин  {g['reason']:<20} {g['detail']}")
    unknown = sum(r["unknown_gaps"] for r in reps)
    print(f"гэпов unknown: {unknown}")
    return 1 if unknown else 0


def cmd_fill_gaps(args) -> int:
    _quiet_http()
    from quant.data import store
    from quant.data.gapfill import fill_gaps

    items = _select(args, default=store.available_symbols())
    rep = fill_gaps([i.hh_symbol for i in items], max_minutes=args.max_minutes)
    print(json.dumps(rep, ensure_ascii=False, default=str, indent=1))
    return 1 if rep["errors"] else 0


def cmd_update(args) -> int:
    """Регулярное обновление: архив за прошлый и текущий месяц → norm → качество → догрузка гэпов → качество.

    Уже загруженные файлы пропускаются, так что ежедневный запуск качает только новые дни. Код возврата 1 — если
    остались ошибки загрузки/сборки или гэпы без причины (их надо разобрать вручную: quant data quality --details).
    """
    _quiet_http()
    from quant.data import store
    from quant.data.backfill import backfill, parse_end
    from quant.data.build import build
    from quant.data.datasets import raw_dir, resolve
    from quant.data.gapfill import fill_gaps
    from quant.data.quality import run_quality
    from quant.timeutil import now_utc

    items = _select(args, default_all=True)
    now = now_utc()
    first_this = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = (first_this - timedelta(days=1)).replace(day=1)          # прошлый месяц — на случай поздних архивов
    rep = backfill(items, resolve(args.datasets), start, parse_end(None), workers=args.workers, keep_zip=False,
                   rest_tail=True, recheck=False, clip_history=True, trades_min_per_lot=50_000)
    a = rep["archive"]
    log.info("update: архив %d файлов, загружено %d (%s МБ), ошибок %d", a["total"], a["downloaded"], a["mb"], a["failed"])
    syms = [i.hh_symbol for i in items if raw_dir("klines_1m", _src(i)).exists()]
    res = build(syms, None, halt_min_bars=args.halt_min_bars)
    build_errors = sorted(hh for hh, r in res.items() if "error" in r)
    unknown = sum(r["unknown_gaps"] for r in run_quality(syms, halt_min_bars=args.halt_min_bars))
    filled = fill_gaps(syms) if unknown else {"filled": 0, "errors": []}
    unknown_after = sum(r["unknown_gaps"] for r in run_quality(syms, halt_min_bars=args.halt_min_bars)) if unknown else 0
    last = max((store.coverage(s)["last_ts"] for s in syms[:5]), default=None) if syms else None
    print(f"update {now:%Y-%m-%d %H:%M} UTC: архив загружено {a['downloaded']} файлов ({a['mb']} МБ), ошибок {a['failed']}; "
          f"сборка {len(res)} символов, ошибок {len(build_errors)}; гэпов unknown {unknown} → догружено "
          f"{filled.get('filled', 0)}, осталось {unknown_after}; данные по {last}")
    for e in a["errors"] + [f"build {hh}: {res[hh]['error'][:120]}" for hh in build_errors] + list(filled.get("errors", [])):
        print(f"  ошибка: {e}")
    return 1 if a["failed"] or build_errors or unknown_after else 0


def cmd_status(args) -> int:
    _quiet_http()
    import polars as pl

    from quant.data import store
    from quant.data.datasets import data_root

    items = _select(args, default=store.available_symbols())
    rows = []
    for inst in items:
        hh = inst.hh_symbol
        c = store.coverage(hh)
        cnt = lambda files: int(pl.scan_parquet(files).select(pl.len()).collect().item()) if files else 0  # noqa: E731
        f_mark = store.period_files(store.mark_dir(hh))
        fp, mp = store.funding_path(hh), store.metrics_path(hh)
        days = (c["last_ts"] - c["first_ts"]).total_seconds() / 86400 if c["rows"] else 0
        norm_mb = sum(_dir_size(store.norm_root() / d / hh) for d in
                      ("bars_1m", "mark_1m", "book_depth", "agg_trades", "live_bars_1m")) + \
            sum(p.stat().st_size for p in (fp, mp) if p.exists())
        rows.append([hh, _fmt_ts(c["first_ts"]), _fmt_ts(c["last_ts"]), f"{days:.0f}", c["rows"],
                     cnt(f_mark), cnt([fp] if fp.exists() else []), cnt([mp] if mp.exists() else []),
                     cnt(store.period_files(store.book_depth_dir(hh))),
                     len(store.period_files(store.agg_trades_dir(hh))), c["halted_min"], c["zero_vol_min"],
                     c["gaps_min"], c["rest_rows"], c["live_rows"], f"{norm_mb / 1e6:.1f}"])
    print(_table(rows, ["symbol", "first_ts", "last_ts", "days", "bars", "mark", "funding", "metrics",
                        "book_depth", "agg_m", "halted", "zero_vol", "gaps_min", "rest", "live", "norm_MB"]))
    root = data_root()
    print(f"диск: raw {_dir_size(root / 'raw') / 1e9:.2f} ГБ, norm {_dir_size(root / 'norm') / 1e9:.2f} ГБ ({root})")
    return 0


def cmd_rebuild_clickhouse(args) -> int:
    _quiet_http()
    from quant.data.clickhouse import ClickHouseLoader, connect

    client = connect(args.url)
    loader = ClickHouseLoader(client, database=args.database, ttl_days=args.ttl_days)
    syms = _split(args.symbols) or None
    tables = _split(args.tables) or None
    rep = loader.rebuild(syms, tables, optimize=not args.no_optimize)
    print(json.dumps(rep, ensure_ascii=False, default=str, indent=1))
    return 1 if rep["mismatches"] else 0


def cmd_ddl(args) -> int:
    from quant.data.clickhouse import schema_sql, write_schema_file

    if args.out:
        write_schema_file(Path(args.out), args.database)
        print(f"записано: {args.out}")
    else:
        print(schema_sql(args.database))
    return 0


def _all_hh():
    from quant.instruments import instruments

    return list(instruments())


def _src(inst) -> str:
    return inst.binance_um




# ---------------------------------------------------------------- регистрация

def register(sub) -> None:
    # quant instruments ...
    p_inst = sub.add_parser("instruments", help="мастер-лист инструментов (3.1)")
    s_inst = p_inst.add_subparsers(dest="cmd", required=True)
    p = s_inst.add_parser("validate", help="сверка instruments.csv с Binance/Bybit (3.1.5)")
    _add_selection(p)
    p.add_argument("--no-divergence", action="store_true", help="не сверять цены Binance<->Bybit")
    p.add_argument("--workers", type=int, default=8)
    p.set_defaults(func=cmd_validate)

    # quant data ...
    p_data = sub.add_parser("data", help="слой данных: backfill, build, качество, ClickHouse")
    s = p_data.add_subparsers(dest="cmd", required=True)

    p = s.add_parser("backfill", help="архив data.binance.vision (+ REST-хвост) -> raw Parquet")
    _add_selection(p)
    p.add_argument("--datasets", default="klines,mark,funding",
                   help="klines,mark,index,funding,metrics,bookDepth,aggTrades")
    p.add_argument("--start", help="YYYY-MM[-DD]; по умолчанию history_start (раньше него не качается)")
    p.add_argument("--end", help="YYYY-MM[-DD] включительно; по умолчанию последний полный день UTC")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--keep-zip", action="store_true")
    p.add_argument("--recheck", action="store_true", help="заново сверить .CHECKSUM уже загруженных файлов")
    p.add_argument("--no-rest-tail", action="store_true", help="не догружать хвост klines/mark через REST")
    p.add_argument("--trades-min-per-lot", type=float, default=50_000,
                   help="aggTrades только для инструментов с max_per_lot_usd >= порога")
    p.add_argument("--no-clip-history", action="store_true",
                   help="качать raw и раньше history_start (в norm всё равно не попадёт)")
    p.set_defaults(func=cmd_backfill)

    p = s.add_parser("build", help="raw -> norm (нормализация, history_start, дедуп, флаги)")
    _add_selection(p)
    p.add_argument("--halt-min-bars", type=int, default=60)
    p.add_argument("--datasets", nargs="+", help="bars_1m,mark_1m,funding,metrics,book_depth,agg_trades")
    p.set_defaults(func=cmd_build)

    p = s.add_parser("quality", help="реестр гэпов и отчёт качества (3.2)")
    _add_selection(p)
    p.add_argument("--halt-min-bars", type=int, default=60)
    p.add_argument("--details", action="store_true", help="вывести открытые записи реестра")
    p.set_defaults(func=cmd_quality)

    p = s.add_parser("fill-gaps", help="догрузка гэпов (unknown/source_outage) через REST")
    _add_selection(p)
    p.add_argument("--max-minutes", type=int, default=60 * 24 * 7)
    p.set_defaults(func=cmd_fill_gaps)

    p = s.add_parser("update", help="регулярное обновление: архив → norm → качество → догрузка гэпов")
    _add_selection(p)
    p.add_argument("--datasets", default="klines,mark,funding")
    p.add_argument("--workers", type=int, default=6)
    p.add_argument("--halt-min-bars", type=int, default=60)
    p.set_defaults(func=cmd_update)

    p = s.add_parser("status", help="покрытие norm по символам")
    _add_selection(p)
    p.set_defaults(func=cmd_status)

    p = s.add_parser("rebuild-clickhouse", help="полная пересборка ClickHouse из norm Parquet")
    p.add_argument("--url", help="DSN ClickHouse (по умолчанию QUANT_CLICKHOUSE_URL)")
    p.add_argument("--database", default="quant")
    p.add_argument("--symbols", nargs="+")
    p.add_argument("--tables", nargs="+", help="bars_1m,mark_1m,funding,metrics,book_depth,agg_trades")
    p.add_argument("--ttl-days", type=int, default=90, help="TTL сделок (agg_trades)")
    p.add_argument("--no-optimize", action="store_true")
    p.set_defaults(func=cmd_rebuild_clickhouse)

    p = s.add_parser("clickhouse-ddl", help="вывести/записать DDL ClickHouse")
    p.add_argument("--database", default="quant")
    p.add_argument("--out", help="файл (например deploy/clickhouse/schema.sql)")
    p.set_defaults(func=cmd_ddl)
