"""Backfill: архив data.binance.vision + REST-хвост → raw Parquet (дальше `quant data build`).

- Архив: monthly для прошлых месяцев, daily для текущего (quant.data.archive.plan_files).
- REST-хвост для klines/mark: дни после последнего архивного файла до конца диапазона
  (архив за вчера публикуется с задержкой) — raw `rest_klines_1m` / `rest_markPriceKlines_1m`, version=2.
- Funding: архив только monthly, поэтому текущий месяц — из REST `/fapi/v1/fundingRate`;
  REST грузится за весь диапазон — в нём есть `markPrice` (у старых записей пустой).
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from quant.data import parse
from quant.data.archive import BinanceArchive, RemoteFile
from quant.data.datasets import (DATASETS, REST_FUNDING, REST_KLINES, REST_MARK, data_root, raw_path)
from quant.data.rest import BinanceREST
from quant.instruments import Instrument
from quant.timeutil import month_range, now_utc, parse_dt, to_ms

log = logging.getLogger(__name__)

REST_TAIL_MAX_DAYS = 7
TRADES_MIN_PER_LOT_USD = 50_000     # aggTrades — только по инструментам с max_per_lot_usd ≥ порога (раздел 3)


def default_end() -> datetime:
    """Конец диапазона по умолчанию (исключительно) — начало текущих суток UTC = последний полный день."""
    n = now_utc()
    return n.replace(hour=0, minute=0, second=0, microsecond=0)


def parse_end(value: str | None) -> datetime:
    """'2026-08' → 2026-09-01, '2026-08-15' → 2026-08-16 (включительно), ISO-время — как есть."""
    if not value:
        return default_end()
    v = value.strip()
    d = parse_dt(v)
    if len(v) == 7:
        return datetime(d.year + (d.month == 12), d.month % 12 + 1, 1, tzinfo=timezone.utc)
    if len(v) == 10:
        return d + timedelta(days=1)
    return d


def symbol_start(inst: Instrument, start: datetime | None, *, clip_history: bool = True) -> datetime:
    """По умолчанию — history_start; явный start раньше history_start подтягивается к нему."""
    if start is None:
        return inst.history_start
    if clip_history and start < inst.history_start:
        return inst.history_start
    return start


# ---------------------------------------------------------------- REST → raw

def _merge_write(df: pl.DataFrame, path: Path, key: str) -> int:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        df = pl.concat([pl.read_parquet(path), df], how="vertical")
    df = df.unique(subset=[key], keep="last").sort(key)
    tmp = path.with_name(path.name + ".tmp")
    df.write_parquet(tmp, compression="zstd", statistics=True)
    tmp.replace(path)
    return df.height


def write_rest_klines(dataset: str, source_symbol: str, raw: pl.DataFrame, *, data_dir: Path | None = None) -> int:
    """Raw REST-бары (вид klines) по дням: rest_*/<SYM>/<SYM>-<dataset>-<YYYY-MM-DD>.parquet (слияние)."""
    if raw.is_empty():
        return 0
    n = 0
    for (day,), part in raw.group_by(pl.col("open_time").dt.date(), maintain_order=True):
        n += _merge_write(part, raw_path(dataset, source_symbol, day.isoformat(), data_dir=data_dir), "open_time")
    return n


def write_rest_funding(source_symbol: str, raw: pl.DataFrame, *, data_dir: Path | None = None) -> int:
    if raw.is_empty():
        return 0
    n = 0
    tagged = raw.with_columns(pl.col("fundingTime").dt.strftime("%Y-%m").alias("_month"))
    for (month,), part in tagged.group_by("_month", maintain_order=True):
        n += _merge_write(part.drop("_month"), raw_path(REST_FUNDING, source_symbol, month, data_dir=data_dir),
                          "fundingTime")
    return n


def rest_klines_range(rest: BinanceREST, dataset: str, source_symbol: str, start: datetime, end: datetime, *,
                      data_dir: Path | None = None) -> int:
    """REST-бары [start, end) → raw (по дням). Возвращает число полученных баров.

    Полные дни, уже лежащие на диске, не перекачиваются.
    """
    kind = "markPriceKlines" if dataset == REST_MARK else "klines"
    total = 0
    day = start.replace(hour=0, minute=0, second=0, microsecond=0)
    while day < end:
        d_end = day + timedelta(days=1)
        s, e = max(day, start), min(d_end, end)
        p = raw_path(dataset, source_symbol, day.date().isoformat(), data_dir=data_dir)
        full = e - s == timedelta(days=1)
        if full and p.exists() and pl.scan_parquet(p).select(pl.len()).collect().item() >= 1440:
            day = d_end
            continue
        rows = rest.klines(source_symbol, to_ms(s), to_ms(e), kind=kind)
        write_rest_klines(dataset, source_symbol, parse.klines_from_rest(rows), data_dir=data_dir)
        total += len(rows)
        day = d_end
    return total


def rest_funding_range(rest: BinanceREST, source_symbol: str, start: datetime, end: datetime, *,
                       data_dir: Path | None = None) -> int:
    """REST funding [start, end) → raw по месяцам. Месяцы, скачанные после своего окончания, пропускаются."""
    need: list[str] = []
    for m in month_range(start, end - timedelta(microseconds=1)):
        y, mo = map(int, m.split("-"))
        m_end = datetime(y + (mo == 12), mo % 12 + 1, 1, tzinfo=timezone.utc)
        p = raw_path(REST_FUNDING, source_symbol, m, data_dir=data_dir)
        if p.exists() and datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc) >= m_end:
            continue
        need.append(m)
    if not need:
        return 0
    y, mo = map(int, need[0].split("-"))
    s = max(start, datetime(y, mo, 1, tzinfo=timezone.utc))
    rows = rest.funding_rate(source_symbol, to_ms(s), to_ms(end))
    return write_rest_funding(source_symbol, parse.funding_from_rest(rows), data_dir=data_dir)


# ---------------------------------------------------------------- оркестрация

def backfill(instruments: list[Instrument], datasets: list[str], start: datetime | None = None,
             end: datetime | None = None, *, workers: int = 6, keep_zip: bool = False, rest_tail: bool = True,
             recheck: bool = False, clip_history: bool = True, data_dir: Path | None = None,
             archive: BinanceArchive | None = None, rest: BinanceREST | None = None,
             trades_min_per_lot: float = TRADES_MIN_PER_LOT_USD) -> dict:
    """Скачать архив (+ REST) по инструментам и датасетам. Возвращает сводку."""
    data_dir = data_root(data_dir)
    end = end or default_end()
    arch = archive or BinanceArchive(data_dir=data_dir, workers=workers, keep_zip=keep_zip)
    arch_ds = [d for d in datasets if d in DATASETS]
    jobs: list[tuple[Instrument, str, datetime]] = []
    skipped_trades = []
    for inst in instruments:
        s = symbol_start(inst, start, clip_history=clip_history)
        for ds in arch_ds:
            if ds == "aggTrades" and inst.max_per_lot_usd < trades_min_per_lot:
                skipped_trades.append(inst.hh_symbol)
                continue
            if s < end:
                jobs.append((inst, ds, s))
    if skipped_trades:
        log.info("aggTrades пропущены (max_per_lot_usd < %g): %s", trades_min_per_lot, ", ".join(skipped_trades))

    # 1. листинги и план (параллельно)
    plans: dict[tuple[str, str], list[RemoteFile]] = {}
    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        futs = {(inst.hh_symbol, ds): ex.submit(arch.plan, ds, inst.binance_um, s, end) for inst, ds, s in jobs}
        for key, fut in futs.items():
            try:
                plans[key] = fut.result()
            except Exception as e:  # noqa: BLE001
                log.error("листинг %s %s: %s", key[0], key[1], e)
                plans[key] = []
    files = [rf for v in plans.values() for rf in v]
    log.info("backfill: %d инструментов, датасеты %s, до %s: %d файлов в плане",
             len(instruments), ",".join(arch_ds), end.isoformat(), len(files))

    # 2. архив
    summary = arch.sync(files, recheck=recheck, label="backfill")
    report: dict = {"archive": summary.as_dict(), "rest": {}, "end": end.isoformat()}

    # 3. REST: хвост klines/mark и funding
    need_rest = (rest_tail and any(d in ("klines_1m", "markPriceKlines_1m") for d in arch_ds)) \
        or "fundingRate" in arch_ds
    if not need_rest:
        return report
    own_rest = rest is None
    rest = rest or BinanceREST()
    now_min = now_utc().replace(second=0, microsecond=0)
    try:
        for inst, ds, s in jobs:
            sym = inst.binance_um
            try:
                if rest_tail and ds in ("klines_1m", "markPriceKlines_1m"):
                    plan = plans.get((inst.hh_symbol, ds), [])
                    covered = max((rf.bounds()[1] for rf in plan), default=s)
                    t0, t1 = max(covered, s), min(end, now_min)
                    if t1 - t0 > timedelta(days=REST_TAIL_MAX_DAYS):
                        log.warning("%s %s: архива нет с %s — REST-хвост ограничен %d днями, остальное — fill-gaps",
                                    inst.hh_symbol, ds, t0.date(), REST_TAIL_MAX_DAYS)
                        t0 = t1 - timedelta(days=REST_TAIL_MAX_DAYS)
                    if t0 < t1:
                        target = REST_KLINES if ds == "klines_1m" else REST_MARK
                        n = rest_klines_range(rest, target, sym, t0, t1, data_dir=data_dir)
                        report["rest"].setdefault(inst.hh_symbol, {})[target] = n
                if ds == "fundingRate":
                    n = rest_funding_range(rest, sym, s, min(end, now_min), data_dir=data_dir)
                    report["rest"].setdefault(inst.hh_symbol, {})[REST_FUNDING] = n
            except Exception as e:  # noqa: BLE001
                log.error("REST %s %s: %s", inst.hh_symbol, ds, e)
                report["rest"].setdefault(inst.hh_symbol, {})["error"] = str(e)
    finally:
        if own_rest:
            rest.close()
    return report
