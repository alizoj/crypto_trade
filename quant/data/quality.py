"""Качество данных (3.2): реестр гэпов 1m с причинами и отчёт по инструменту.

Гэп — отсутствующие минуты между соседними барами norm-ряда (архив + REST; live — отдельно).
Реестр ведётся для last-баров (dataset klines_1m) и mark 1m (markPriceKlines_1m): mark — это equity и
лимиты propsim, а архив markPriceKlines теряет целые дни чаще, чем klines.
Причины:
- listing — пропуск в начале истории в окне listing_window_min после history_start (в т.ч. от
  history_start до первого бара). Более позднее начало ряда — не гэп, а history_lag_min в отчёте;
- relisting — пропуск, примыкающий к halted-серии (делистинг → пауза → возобновление);
- exchange_maintenance — одновременный гэп у ≥ maintenance_share (80%) символов, имеющих данные в этот
  момент (не меньше maintenance_min_symbols), либо биржа по REST подтвердила отсутствие баров (gapfill);
- source_outage — пропуски в live-части (после последнего архивного/REST-бара);
- halted — сами halted-серии (регистрируются как отдельные записи, это не отсутствие баров);
- unknown — всё остальное. Приёмка «0 необъяснённых гэпов» = нет записей unknown.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl
from sqlalchemy import select

from quant.data import store
from quant.data.datasets import SOURCE_BINANCE_UM
from quant.data.flags import HALT_MIN_BARS, halted_runs
from quant.db import session_scope
from quant.db.models import GapRecord, InstrumentStatus, utcnow
from quant.instruments import by_symbol

log = logging.getLogger(__name__)

DATASET = "klines_1m"
MARK_DATASET = "markPriceKlines_1m"
GAP_DATASETS = (DATASET, MARK_DATASET)
REASONS = ("listing", "relisting", "exchange_maintenance", "source_outage", "halted", "unknown")
MIN = timedelta(minutes=1)


@dataclass
class Gap:
    hh_symbol: str
    start: datetime          # первая отсутствующая минута (для halted — первая halted-минута)
    end: datetime            # последняя отсутствующая минута (включительно)
    reason: str = "unknown"
    detail: str = ""
    source: str = SOURCE_BINANCE_UM
    dataset: str = DATASET

    @property
    def minutes(self) -> int:
        return int((self.end - self.start) / MIN) + 1


@dataclass
class SymbolScan:
    hh_symbol: str
    first_ts: datetime | None
    last_ts: datetime | None            # последний архивный/REST бар
    gaps: list[Gap] = field(default_factory=list)


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def missing_ranges(ts: pl.Series) -> list[tuple[datetime, datetime]]:
    """[(первая, последняя) отсутствующие минуты] между соседними метками (ts отсортирован, уникален)."""
    if ts.len() < 2:
        return []
    df = pl.DataFrame({"ts": ts}).with_columns(pl.col("ts").shift(1).alias("prev"))
    g = df.filter((pl.col("ts") - pl.col("prev")) > pl.duration(minutes=1))
    return [(p + MIN, t - MIN) for p, t in zip(g.get_column("prev").to_list(), g.get_column("ts").to_list())]


def _read(files: list[Path], cols: list[str]) -> pl.DataFrame:
    return pl.scan_parquet(files).select(cols).collect().unique(subset=["ts"]).sort("ts") if files \
        else pl.DataFrame(schema={c: pl.Datetime("ms", "UTC") if c == "ts" else pl.Float64 for c in cols})


def scan_symbol(hh: str, *, dataset: str = DATASET, include_live: bool = True, halt_min_bars: int = HALT_MIN_BARS,
                listing_window_min: int = 1440, data_dir: Path | None = None) -> SymbolScan:
    """Найти гэпы (и halted-серии для klines) символа и проставить локальные причины.

    Межсимвольная эвристика exchange_maintenance — в classify_maintenance.
    """
    inst = by_symbol().get(hh)
    hs = inst.history_start if inst else None
    is_bars = dataset == DATASET
    if is_bars:
        data = _read(store.period_files(store.bars_dir(hh, data_dir)), ["ts", "open", "high", "low", "close", "volume"])
    else:
        data = _read(store.period_files(store.mark_dir(hh, data_dir)), ["ts"])
    scan = SymbolScan(hh, None, None)
    if data.is_empty():
        return scan
    ts = data.get_column("ts")
    scan.first_ts, scan.last_ts = ts.min(), ts.max()
    run_edges: list[tuple[datetime, datetime]] = []
    if is_bars:
        runs = halted_runs(data, halt_min_bars)
        run_edges = [(r["start_ts"], r["end_ts"]) for r in runs.iter_rows(named=True)]
        for r in runs.iter_rows(named=True):
            scan.gaps.append(Gap(hh, r["start_ts"], r["end_ts"], "halted",
                                 f"замороженные бары: volume=0, O=H=L=C={r['price']}", dataset=dataset))
    listing_edge = (hs + timedelta(minutes=listing_window_min)) if hs is not None else None
    # пропуск от history_start до первого бара — только в окне листинга; более позднее начало ряда —
    # это не скачанная история (см. history_lag_min в отчёте), а не пропуск в данных
    if hs is not None and hs < scan.first_ts <= listing_edge:
        scan.gaps.append(Gap(hh, hs, scan.first_ts - MIN, "listing", "первый бар позже history_start",
                             dataset=dataset))
    for s, e in missing_ranges(ts):
        g = Gap(hh, s, e, dataset=dataset)
        if any(re_ == s - MIN or rs == e + MIN for rs, re_ in run_edges):
            g.reason, g.detail = "relisting", "примыкает к halted-серии"
        elif listing_edge is not None and s < listing_edge:
            g.reason, g.detail = "listing", f"в пределах {listing_window_min} мин после history_start"
        scan.gaps.append(g)
    if include_live and is_bars:
        live = _read(store.period_files(store.live_dir(hh, data_dir)), ["ts"])
        live = live.filter(pl.col("ts") > scan.last_ts)
        if live.height:
            lts = pl.concat([pl.Series("ts", [scan.last_ts], dtype=live.schema["ts"]), live.get_column("ts")])
            for s, e in missing_ranges(lts):
                scan.gaps.append(Gap(hh, s, e, "source_outage", "пропуск в live-потоке", dataset=dataset))
    return scan


def classify_maintenance(scans: dict[str, SymbolScan], *, share: float = 0.8, min_symbols: int = 3) -> None:
    """unknown → exchange_maintenance, если гэп одновременно у ≥ share символов, имеющих данные в этот момент."""
    real = {hh: [g for g in sc.gaps if g.reason != "halted"] for hh, sc in scans.items()}
    for hh, sc in scans.items():
        for g in sc.gaps:
            if g.reason != "unknown":
                continue
            peers = [p for p, ps in scans.items()
                     if ps.first_ts is not None and ps.first_ts < g.start and ps.last_ts > g.end]
            if hh not in peers:
                peers.append(hh)
            hit = [p for p in peers if any(x.start <= g.end and x.end >= g.start for x in real[p])]
            if len(peers) >= min_symbols and len(hit) / len(peers) >= share:
                g.reason = "exchange_maintenance"
                g.detail = f"одновременный гэп у {len(hit)}/{len(peers)} символов с данными"


def detect_gaps(symbols: list[str] | None = None, *, datasets: tuple[str, ...] = GAP_DATASETS,
                include_live: bool = True, halt_min_bars: int = HALT_MIN_BARS, maintenance_share: float = 0.8,
                maintenance_min_symbols: int = 3, data_dir: Path | None = None) -> dict[str, list[Gap]]:
    """Гэпы по символам (все датасеты вместе). Для эвристики maintenance сканируются все символы norm."""
    universe = store.available_symbols(data_dir=data_dir)
    wanted = [s for s in (symbols or universe) if s in universe]
    out: dict[str, list[Gap]] = {hh: [] for hh in wanted}
    for ds in datasets:
        scans = {hh: scan_symbol(hh, dataset=ds, include_live=include_live, halt_min_bars=halt_min_bars,
                                 data_dir=data_dir) for hh in universe}
        classify_maintenance(scans, share=maintenance_share, min_symbols=maintenance_min_symbols)
        for hh in wanted:
            out[hh] += scans[hh].gaps
    return out


# ---------------------------------------------------------------- реестр (БД)

def register_gaps(gaps: dict[str, list[Gap]], *, datasets: tuple[str, ...] = GAP_DATASETS) -> dict[str, int]:
    """Синхронизировать реестр: новые — добавить, найденные снова — обновить, исчезнувшие гэпы —
    отметить filled (данные появились), исчезнувшие halted — удалить. Ключ — (hh, source, dataset, start)."""
    stats = {"added": 0, "updated": 0, "closed": 0, "removed": 0}
    with session_scope() as s:
        for hh, items in gaps.items():
            rows = s.scalars(select(GapRecord).where(GapRecord.hh_symbol == hh,
                                                     GapRecord.dataset.in_(datasets))).all()
            existing = {(r.source, r.dataset, _aware(r.start_ts)): r for r in rows}
            seen = set()
            for g in items:
                key = (g.source, g.dataset, g.start)
                seen.add(key)
                r = existing.get(key)
                if r is None:
                    s.add(GapRecord(hh_symbol=hh, source=g.source, dataset=g.dataset, start_ts=g.start,
                                    end_ts=g.end, minutes=g.minutes, reason=g.reason, detail=g.detail,
                                    filled=False, detected_at=utcnow()))
                    stats["added"] += 1
                    continue
                r.end_ts, r.minutes = g.end, g.minutes
                rest_confirmed = "REST:" in (r.detail or "") and r.reason != "unknown"
                if not rest_confirmed:
                    r.reason, r.detail = g.reason, g.detail
                # причина, подтверждённая REST (gapfill), не перетирается эвристикой
                r.filled = False
                stats["updated"] += 1
            for key, r in existing.items():
                if key in seen:
                    continue
                if r.reason == "halted":
                    s.delete(r)
                    stats["removed"] += 1
                elif not r.filled:
                    r.filled = True
                    r.detail = (r.detail + "; " if r.detail else "") + "закрыт: бары появились"
                    stats["closed"] += 1
    return stats


def load_registry(symbols: list[str] | None = None, *, open_only: bool = False,
                  datasets: tuple[str, ...] = GAP_DATASETS) -> list[dict]:
    with session_scope() as s:
        q = select(GapRecord).where(GapRecord.dataset.in_(datasets))
        if symbols:
            q = q.where(GapRecord.hh_symbol.in_(symbols))
        if open_only:
            q = q.where(GapRecord.filled.is_(False))
        q = q.order_by(GapRecord.hh_symbol, GapRecord.dataset, GapRecord.start_ts)
        return [{"id": r.id, "hh_symbol": r.hh_symbol, "source": r.source, "dataset": r.dataset,
                 "start_ts": _aware(r.start_ts), "end_ts": _aware(r.end_ts), "minutes": r.minutes,
                 "reason": r.reason, "detail": r.detail, "filled": r.filled} for r in s.scalars(q)]


# ---------------------------------------------------------------- live vs архив (3.2.5)

def live_vs_archive(hh: str, start: datetime | None = None, end: datetime | None = None, *,
                    rel_tol: float = 1e-9, data_dir: Path | None = None) -> dict:
    """Сверка предварительных live-баров (version=1) с архивом/REST на T+1.

    Возвращает число сверенных минут, расхождения по OHLCV (относительный допуск) и минуты live,
    которых нет в архиве. При чтении (load_bars) архив и так перекрывает live — это отчёт о качестве потока.
    """
    cols = ["ts", "open", "high", "low", "close", "volume"]
    live = store._scan(store.period_files(store.live_dir(hh, data_dir), start, end), start, end)
    arch = store._scan(store.period_files(store.bars_dir(hh, data_dir), start, end), start, end)
    out = {"hh_symbol": hh, "compared": 0, "mismatched": 0, "live_only": 0, "examples": []}
    if live is None or live.is_empty() or arch is None or arch.is_empty():
        return out
    j = live.select(cols).join(arch.select(cols), on="ts", how="left", suffix="_a")
    out["live_only"] = int(j.get_column("open_a").null_count())
    j = j.filter(pl.col("open_a").is_not_null())
    out["compared"] = j.height
    diff = pl.lit(False)
    for c in cols[1:]:
        diff = diff | ((pl.col(c) - pl.col(f"{c}_a")).abs() > rel_tol * pl.max_horizontal(pl.col(f"{c}_a").abs(), 1e-12))
    bad = j.filter(diff)
    out["mismatched"] = bad.height
    out["examples"] = bad.head(5).to_dicts()
    return out


# ---------------------------------------------------------------- aggTrades (3.2.6)

def agg_trades_continuity(hh: str, start: datetime, end: datetime, *, data_dir: Path | None = None) -> dict:
    """Полнота сделок по непрерывности agg_id и first/last trade id (а не по времени)."""
    df = store.load_agg_trades(hh, start, end, data_dir=data_dir)
    out = {"hh_symbol": hh, "trades": df.height, "agg_id_gaps": 0, "agg_id_missing": 0, "trade_id_gaps": 0,
           "first_ts": None, "last_ts": None}
    if df.height < 2:
        return out
    d = df.select(
        (pl.col("agg_id").diff() - 1).alias("agg_skip"),
        (pl.col("first_id") - pl.col("last_id").shift(1) - 1).alias("trade_skip"),
    ).drop_nulls()
    out.update(
        agg_id_gaps=int((d["agg_skip"] != 0).sum()),
        agg_id_missing=int(d["agg_skip"].clip(lower_bound=0).sum()),
        trade_id_gaps=int((d["trade_skip"] != 0).sum()),
        first_ts=df["ts"].min(), last_ts=df["ts"].max(),
    )
    return out


# ---------------------------------------------------------------- отчёт качества

def quality_report(hh: str, *, data_dir: Path | None = None) -> dict:
    """Доли halted / zero_vol / synthetic, гэпы по причинам (открытые), расхождение с Bybit (3.2.8)."""
    cov = store.coverage(hh, data_dir=data_dir)
    files = store.period_files(store.bars_dir(hh, data_dir))
    synthetic = 0
    if files:
        synthetic = int(pl.scan_parquet(files).select(pl.col("synthetic").sum()).collect().item() or 0)
    rows = cov["rows"] or 0
    by_reason: dict[str, dict[str, int]] = {}
    mark_by_reason: dict[str, dict[str, int]] = {}
    for g in load_registry([hh]):
        if g["filled"]:
            continue
        target = by_reason if g["dataset"] == DATASET else mark_by_reason
        d = target.setdefault(g["reason"], {"count": 0, "minutes": 0})
        d["count"] += 1
        d["minutes"] += g["minutes"]
    with session_scope() as s:
        st = s.get(InstrumentStatus, hh)
        div = st.divergence_median_pct if st else None
        suspect = st.mapping_suspect if st else None
    inst = by_symbol().get(hh)
    lag = None
    if inst is not None and cov["first_ts"] is not None:
        lag = max(0, int((cov["first_ts"] - inst.history_start) / MIN))
    unknown = by_reason.get("unknown", {}).get("count", 0) + mark_by_reason.get("unknown", {}).get("count", 0)
    return {
        "hh_symbol": hh, "first_ts": cov["first_ts"], "last_ts": cov["last_ts"], "rows": rows,
        "history_lag_min": lag,                      # начало ряда позже history_start (не скачано/нет архива)
        "halted_pct": 100.0 * cov["halted_min"] / rows if rows else 0.0,
        "zero_vol_pct": 100.0 * cov["zero_vol_min"] / rows if rows else 0.0,
        "synthetic_pct": 100.0 * synthetic / rows if rows else 0.0,
        "missing_min": cov["gaps_min"], "gaps": by_reason, "mark_gaps": mark_by_reason,
        "unknown_gaps": unknown,
        "bybit_divergence_median_pct": div, "mapping_suspect": suspect,
    }


def run_quality(symbols: list[str] | None = None, *, halt_min_bars: int = HALT_MIN_BARS,
                data_dir: Path | None = None) -> list[dict]:
    """Детект → реестр → отчёты по символам."""
    gaps = detect_gaps(symbols, halt_min_bars=halt_min_bars, data_dir=data_dir)
    stats = register_gaps(gaps)
    log.info("реестр гэпов: %s", stats)
    return [quality_report(hh, data_dir=data_dir) for hh in gaps]
