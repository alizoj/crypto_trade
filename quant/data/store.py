"""Нормализованный слой (norm Parquet): чтение для research/backtest/live и запись live-баров.

Раскладка (data_dir/norm/...):
    bars_1m/<hh>/<YYYY>.parquet          BARS_1M, version 3 (архив) / 2 (REST)
    mark_1m/<hh>/<YYYY>.parquet          MARK_1M (+ version)
    funding/<hh>.parquet                 FUNDING (+ interval_hours)
    metrics/<hh>.parquet                 ts + метрики OI / long-short (5m)
    book_depth/<hh>/<YYYY>.parquet       ts, percentage, depth, notional
    agg_trades/<hh>/<YYYY-MM>.parquet    ts, agg_id, price, qty, first_id, last_id, is_buyer_maker
    live_bars_1m/<hh>/<YYYY-MM-DD>.parquet   BARS_1M, version 1 (предварительные, WS)
Всё нормализовано за 1 единицу базового актива (3.1.2) и начинается не раньше history_start (3.2.1).
В key-value метаданных каждого файла — sha256 содержимого, число строк и диапазон ts
(для детерминированного dataset_hash без чтения всех данных).
"""

from __future__ import annotations

import hashlib
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import polars as pl

from quant.data.aggregate import dedup_1m
from quant.data.datasets import SOURCE_BINANCE_UM, VERSION_WS, data_root
from quant.data.flags import HALT_MIN_BARS, add_flags
from quant.schemas import BARS_1M, FUNDING, MARK_1M, TS, conform
from quant.timeutil import parse_dt

META_SHA = "quant.sha256"
META_ROWS = "quant.rows"
META_MIN = "quant.min_ts_ms"
META_MAX = "quant.max_ts_ms"

METRICS_COLS = {
    "ts": TS,
    "sum_open_interest": pl.Float64,             # в базовом активе (нормализовано)
    "sum_open_interest_value": pl.Float64,       # USDT
    "count_toptrader_long_short_ratio": pl.Float64,
    "sum_toptrader_long_short_ratio": pl.Float64,
    "count_long_short_ratio": pl.Float64,
    "sum_taker_long_short_vol_ratio": pl.Float64,
}
BOOK_DEPTH_COLS = {"ts": TS, "percentage": pl.Float64, "depth": pl.Float64, "notional": pl.Float64}
AGG_TRADES_COLS = {
    "ts": TS, "agg_id": pl.Int64, "price": pl.Float64, "qty": pl.Float64,
    "first_id": pl.Int64, "last_id": pl.Int64, "is_buyer_maker": pl.Boolean,
}
MARK_STORE = {**MARK_1M, "version": pl.Int8}
FUNDING_STORE = {**FUNDING, "interval_hours": pl.Int16}


# ---------------------------------------------------------------- пути

def norm_root(data_dir: Path | None = None) -> Path:
    return data_root(data_dir) / "norm"


def bars_dir(hh: str, data_dir: Path | None = None) -> Path:
    return norm_root(data_dir) / "bars_1m" / hh


def mark_dir(hh: str, data_dir: Path | None = None) -> Path:
    return norm_root(data_dir) / "mark_1m" / hh


def live_dir(hh: str, data_dir: Path | None = None) -> Path:
    return norm_root(data_dir) / "live_bars_1m" / hh


def funding_path(hh: str, data_dir: Path | None = None) -> Path:
    return norm_root(data_dir) / "funding" / f"{hh}.parquet"


def metrics_path(hh: str, data_dir: Path | None = None) -> Path:
    return norm_root(data_dir) / "metrics" / f"{hh}.parquet"


def book_depth_dir(hh: str, data_dir: Path | None = None) -> Path:
    return norm_root(data_dir) / "book_depth" / hh


def agg_trades_dir(hh: str, data_dir: Path | None = None) -> Path:
    return norm_root(data_dir) / "agg_trades" / hh


# ---------------------------------------------------------------- время и диапазоны

def _dt(v: datetime | str | None) -> datetime | None:
    d = parse_dt(v) if v is not None else None
    return d.astimezone(timezone.utc) if d is not None else None


def _history_start(hh: str) -> datetime | None:
    from quant.instruments import by_symbol

    inst = by_symbol().get(hh)
    return inst.history_start if inst else None


def _period_bounds(name: str) -> tuple[datetime, datetime] | None:
    """'2024' → год, '2024-05' → месяц, '2024-05-07' → день. [start, end)."""
    stem = name.removesuffix(".parquet")
    if re.fullmatch(r"\d{4}", stem):
        y = int(stem)
        return datetime(y, 1, 1, tzinfo=timezone.utc), datetime(y + 1, 1, 1, tzinfo=timezone.utc)
    if re.fullmatch(r"\d{4}-\d{2}", stem):
        y, m = map(int, stem.split("-"))
        s = datetime(y, m, 1, tzinfo=timezone.utc)
        e = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=timezone.utc)
        return s, e
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", stem):
        s = datetime.strptime(stem, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return s, s + timedelta(days=1)
    return None


def period_files(d: Path, start: datetime | None = None, end: datetime | None = None) -> list[Path]:
    """Файлы-периоды каталога, пересекающиеся с [start, end)."""
    if not d.exists():
        return []
    out = []
    for p in sorted(d.glob("*.parquet")):
        b = _period_bounds(p.name)
        if b is None:
            continue
        if start is not None and b[1] <= start:
            continue
        if end is not None and b[0] >= end:
            continue
        out.append(p)
    return out


def _ts_filter(start: datetime | None, end: datetime | None, col: str = "ts") -> pl.Expr:
    e = pl.lit(True)
    if start is not None:
        e = e & (pl.col(col) >= start)
    if end is not None:
        e = e & (pl.col(col) < end)
    return e


def _scan(files: list[Path], start: datetime | None, end: datetime | None) -> pl.DataFrame | None:
    """Прочитать файлы с фильтром по ts (predicate pushdown по статистикам Parquet)."""
    if not files:
        return None
    flt = _ts_filter(start, end)
    try:
        return pl.scan_parquet(files).filter(flt).collect()
    except (pl.exceptions.SchemaError, pl.exceptions.ColumnNotFoundError):
        # файлы разных версий схемы — склеиваем по именам колонок
        return pl.concat([pl.scan_parquet(f).filter(flt).collect() for f in files], how="diagonal_relaxed")


def _lo(hh: str, start: datetime | None) -> datetime | None:
    hs = _history_start(hh)
    if hs is None:
        return start
    return hs if start is None or start < hs else start


# ---------------------------------------------------------------- хэш содержимого и запись

def frame_digest(df: pl.DataFrame) -> str:
    """sha256 содержимого кадра (стабилен между версиями Polars: хэшируются сырые значения)."""
    h = hashlib.sha256()
    h.update(f"rows={df.height}".encode())
    for name in df.columns:
        s = df.get_column(name)
        h.update(f"|{name}:{s.dtype}".encode())
        if s.null_count():
            h.update(s.is_null().to_numpy().tobytes())
        dt = s.dtype
        if dt == pl.Utf8 or dt == pl.Categorical:
            h.update(s.cast(pl.Utf8).fill_null("").str.join("\x1f").item().encode())
        elif dt == pl.Boolean:
            h.update(s.fill_null(False).cast(pl.UInt8).to_numpy().tobytes())
        elif dt.is_temporal():
            h.update(s.to_physical().fill_null(0).to_numpy().tobytes())
        elif dt.is_numeric():
            h.update(s.fill_null(0).to_numpy().tobytes())
        else:
            h.update(s.cast(pl.Utf8).fill_null("").str.join("\x1f").item().encode())
    return h.hexdigest()


def _replace(tmp: Path, path: Path) -> None:
    for attempt in range(20):   # Windows: файл может быть открыт читателем
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.25 * (attempt + 1))
    os.replace(tmp, path)


def write_norm(df: pl.DataFrame, path: Path, *, digest: bool = True) -> dict:
    """Атомарная запись norm-файла с метаданными (sha256, rows, min/max ts)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    meta: dict[str, str] = {META_ROWS: str(df.height)}
    if "ts" in df.columns and df.height:
        ts = df.get_column("ts").dt.epoch("ms")
        meta[META_MIN] = str(ts.min())
        meta[META_MAX] = str(ts.max())
    if digest:
        meta[META_SHA] = frame_digest(df)
    tmp = path.with_suffix(".parquet.tmp")
    df.write_parquet(tmp, compression="zstd", statistics=True, metadata=meta)
    _replace(tmp, path)
    return meta


def read_meta(path: Path) -> dict[str, str]:
    try:
        return {k: v for k, v in pl.read_parquet_metadata(path).items() if k.startswith("quant.")}
    except Exception:  # noqa: BLE001
        return {}


def file_digest(path: Path, start: datetime | None = None, end: datetime | None = None) -> tuple[str, int]:
    """Хэш использованных строк файла: из метаданных, если файл целиком в [start, end), иначе по строкам."""
    meta = read_meta(path)
    inside = META_SHA in meta and META_MIN in meta
    if inside:
        lo, hi = int(meta[META_MIN]), int(meta[META_MAX])
        if start is not None and lo < int(start.timestamp() * 1000):
            inside = False
        if end is not None and hi >= int(end.timestamp() * 1000):
            inside = False
    if inside:
        return meta[META_SHA], int(meta[META_ROWS])
    df = pl.scan_parquet(path).filter(_ts_filter(start, end)).collect()
    return frame_digest(df), df.height


# ---------------------------------------------------------------- чтение

def load_bars(hh_symbol: str, start: datetime | None = None, end: datetime | None = None, *,
              with_mark: bool = True, include_live: bool = True, data_dir: Path | None = None) -> pl.DataFrame:
    """1m-бары основного источника (BARS_1M [+ m_open..m_close]).

    Отсортировано по ts, дедуп по version (live version=1 перекрывается архивом/REST),
    только ts >= history_start, end — исключительно.
    """
    start, end = _dt(start), _dt(end)
    lo = _lo(hh_symbol, start)
    frames = []
    df = _scan(period_files(bars_dir(hh_symbol, data_dir), lo, end), lo, end)
    if df is not None and df.height:
        frames.append(conform(df, BARS_1M, extra=False))
    has_live = False
    if include_live:
        live = _scan(period_files(live_dir(hh_symbol, data_dir), lo, end), lo, end)
        if live is not None and live.height:
            frames.append(conform(live, BARS_1M, extra=False))
            has_live = True
    if not frames:
        out = pl.DataFrame(schema=BARS_1M)
    else:
        out = dedup_1m(pl.concat(frames, how="vertical"))
        if has_live and (out.get_column("version") == VERSION_WS).any():
            # halted для предварительных баров считаем по склеенному ряду (архив + live)
            stored = out.get_column("halted").fill_null(False)
            flagged = add_flags(out.drop("halted", "zero_vol"), HALT_MIN_BARS)
            out = flagged.with_columns((pl.col("halted") | stored).alias("halted"))
        out = conform(out, BARS_1M, extra=False)
    if with_mark:
        mk = load_mark(hh_symbol, lo, end, data_dir=data_dir).rename(
            {"open": "m_open", "high": "m_high", "low": "m_low", "close": "m_close"})
        out = out.join(mk, on="ts", how="left")
    return out.sort("ts")


def load_mark(hh_symbol: str, start: datetime | None = None, end: datetime | None = None, *,
              data_dir: Path | None = None) -> pl.DataFrame:
    start, end = _dt(start), _dt(end)
    lo = _lo(hh_symbol, start)
    df = _scan(period_files(mark_dir(hh_symbol, data_dir), lo, end), lo, end)
    if df is None or df.is_empty():
        return pl.DataFrame(schema=MARK_1M)
    if "version" in df.columns:
        df = dedup_1m(df)
    return conform(df, MARK_1M, extra=False).sort("ts")


def load_funding(hh_symbol: str, start: datetime | None = None, end: datetime | None = None, *,
                 data_dir: Path | None = None) -> pl.DataFrame:
    start, end = _dt(start), _dt(end)
    lo = _lo(hh_symbol, start)
    p = funding_path(hh_symbol, data_dir)
    if not p.exists():
        return pl.DataFrame(schema=FUNDING)
    df = pl.scan_parquet(p).filter(_ts_filter(lo, end)).collect()
    return conform(df, FUNDING, extra=False).sort("ts")


def load_metrics(hh_symbol: str, start: datetime | None = None, end: datetime | None = None, *,
                 data_dir: Path | None = None) -> pl.DataFrame:
    start, end = _dt(start), _dt(end)
    lo = _lo(hh_symbol, start)
    p = metrics_path(hh_symbol, data_dir)
    if not p.exists():
        return pl.DataFrame(schema=METRICS_COLS)
    df = pl.scan_parquet(p).filter(_ts_filter(lo, end)).collect()
    return conform(df, METRICS_COLS, extra=False).sort("ts")


def load_book_depth(hh_symbol: str, start: datetime | None = None, end: datetime | None = None, *,
                    data_dir: Path | None = None) -> pl.DataFrame:
    start, end = _dt(start), _dt(end)
    lo = _lo(hh_symbol, start)
    df = _scan(period_files(book_depth_dir(hh_symbol, data_dir), lo, end), lo, end)
    if df is None or df.is_empty():
        return pl.DataFrame(schema=BOOK_DEPTH_COLS)
    return conform(df, BOOK_DEPTH_COLS, extra=False).sort(["ts", "percentage"])


def load_agg_trades(hh_symbol: str, start: datetime, end: datetime, *,
                    data_dir: Path | None = None) -> pl.DataFrame:
    """Сделки [start, end) по возрастанию agg_id. is_buyer_maker=True ⇒ агрессор продавал."""
    start, end = _dt(start), _dt(end)
    lo = _lo(hh_symbol, start)
    df = _scan(period_files(agg_trades_dir(hh_symbol, data_dir), lo, end), lo, end)
    if df is None or df.is_empty():
        return pl.DataFrame(schema=AGG_TRADES_COLS)
    return conform(df, AGG_TRADES_COLS, extra=False).sort("agg_id")


# ---------------------------------------------------------------- live

def write_live_bars(hh_symbol: str, df: pl.DataFrame, *, data_dir: Path | None = None) -> None:
    """Предварительные бары WS (version=1) в norm/live_bars_1m/<hh>/<YYYY-MM-DD>.parquet.

    df — уже нормализованные бары (за 1 единицу базового актива, см. quant.data.build.normalize_klines)
    с колонками BARS_1M (минимум ts + OHLCV). Повторная запись того же ts заменяет строку.
    На T+1 их перекрывает архив (version 3) при чтении load_bars.
    """
    if df.is_empty():
        return
    hs = _history_start(hh_symbol)
    new = df.with_columns(pl.col("ts").cast(TS))
    if hs is not None:
        new = new.filter(pl.col("ts") >= hs)
    if new.is_empty():
        return
    new = new.with_columns(
        pl.lit(VERSION_WS, dtype=pl.Int8).alias("version"),
        (pl.col("source") if "source" in new.columns else pl.lit(SOURCE_BINANCE_UM)).fill_null(SOURCE_BINANCE_UM)
        .alias("source"),
        (pl.col("synthetic").fill_null(False) if "synthetic" in new.columns else pl.lit(False)).alias("synthetic"),
        (pl.col("volume") == 0).fill_null(False).alias("zero_vol"),
        (pl.col("halted").fill_null(False) if "halted" in new.columns else pl.lit(False)).alias("halted"),
    )
    new = conform(new, BARS_1M, extra=False)
    d = live_dir(hh_symbol, data_dir)
    for (day,), part in new.group_by(pl.col("ts").dt.date(), maintain_order=True):
        path = d / f"{day.isoformat()}.parquet"
        if path.exists():
            old = conform(pl.read_parquet(path), BARS_1M, extra=False)
            part = pl.concat([old, part], how="vertical")
        part = part.unique(subset=["ts"], keep="last", maintain_order=True).sort("ts")
        write_norm(part, path)


# ---------------------------------------------------------------- обзор

def available_symbols(*, data_dir: Path | None = None) -> list[str]:
    root = norm_root(data_dir) / "bars_1m"
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir() and any(p.glob("*.parquet")))


def coverage(hh_symbol: str, *, data_dir: Path | None = None) -> dict:
    """first_ts, last_ts, rows, halted_min, zero_vol_min, gaps_min (+ live_rows, live_last_ts)."""
    files = period_files(bars_dir(hh_symbol, data_dir))
    out = {"hh_symbol": hh_symbol, "first_ts": None, "last_ts": None, "rows": 0, "halted_min": 0,
           "zero_vol_min": 0, "gaps_min": 0, "rest_rows": 0, "live_rows": 0, "live_last_ts": None}
    if files:
        agg = pl.scan_parquet(files).select(
            pl.col("ts").min().alias("first_ts"), pl.col("ts").max().alias("last_ts"),
            pl.len().alias("rows"), pl.col("halted").sum().alias("halted_min"),
            pl.col("zero_vol").sum().alias("zero_vol_min"),
            (pl.col("version") == 2).sum().alias("rest_rows"),
        ).collect().row(0, named=True)
        out.update(agg)
        if agg["rows"]:
            span = int((agg["last_ts"] - agg["first_ts"]).total_seconds() // 60) + 1
            out["gaps_min"] = span - int(agg["rows"])
    lfiles = period_files(live_dir(hh_symbol, data_dir))
    if lfiles:
        la = pl.scan_parquet(lfiles).select(pl.len().alias("n"), pl.col("ts").max().alias("mx")).collect().row(0)
        out["live_rows"], out["live_last_ts"] = int(la[0]), la[1]
    for k in ("rows", "halted_min", "zero_vol_min", "gaps_min", "rest_rows"):
        out[k] = int(out[k] or 0)
    return out


def dataset_hash(symbols: list[str], start: datetime | None = None, end: datetime | None = None, *,
                 data_dir: Path | None = None) -> str:
    """Детерминированный хэш использованных norm-данных (bars, mark, funding, live) по содержимому.

    Не зависит от mtime и абсолютных путей: файлы целиком внутри [start, end) берут sha256 из
    метаданных, граничные — хэшируются по фактически попадающим в диапазон строкам.
    """
    start, end = _dt(start), _dt(end)
    h = hashlib.sha256()
    h.update(f"dataset_hash/v1|{start.isoformat() if start else ''}|{end.isoformat() if end else ''}".encode())
    for hh in sorted(set(symbols)):
        hs = _history_start(hh)
        lo = _lo(hh, start)
        h.update(f"#{hh}|hs={hs.isoformat() if hs else ''}".encode())
        groups = [
            ("bars_1m", period_files(bars_dir(hh, data_dir), lo, end)),
            ("mark_1m", period_files(mark_dir(hh, data_dir), lo, end)),
            ("funding", [p for p in [funding_path(hh, data_dir)] if p.exists()]),
            ("live_bars_1m", period_files(live_dir(hh, data_dir), lo, end)),
        ]
        for name, files in groups:
            for f in files:
                dg, rows = file_digest(f, lo, end)
                if rows:
                    h.update(f"|{name}/{f.name}:{rows}:{dg}".encode())
    return h.hexdigest()
