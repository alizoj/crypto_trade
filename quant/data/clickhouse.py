"""ClickHouse: DDL, загрузка из norm Parquet и полная пересборка (раздел 4).

- ReplacingMergeTree(version), PARTITION BY toYYYYMM(ts), ORDER BY (hh_symbol, source, ts[, ключ]);
  колонки hh_symbol, source, source_symbol в каждой строке (правило 3.1.1).
- Кодеки: DoubleDelta+ZSTD для ts, Gorilla+ZSTD для Float64.
- Materialized views НЕ создаются: MV срабатывает на каждую вставку, а ReplacingMergeTree схлопывает
  дубли только при слиянии — повторная загрузка/догрузка гэпов навсегда задвоила бы volume/CVD старших ТФ.
  Старшие ТФ считаются в quant (quant.data.aggregate) из дедуплицированных 1m; для UI — запросы с FINAL.
- Сделки удаляются по TTL через N дней: источник истины — Parquet.
- Источник истины — Parquet; `rebuild` пересоздаёт таблицы и заливает всё заново.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any, Protocol

import polars as pl
import pyarrow as pa

from quant.data import store
from quant.data.datasets import SOURCE_BINANCE_UM, VERSION_ARCHIVE
from quant.instruments import by_symbol
from quant.timeutil import now_utc

log = logging.getLogger(__name__)

DEFAULT_DATABASE = "quant"
AGG_TRADES_TTL_DAYS = 90

TS_CODEC = "CODEC(DoubleDelta, ZSTD)"
F64_CODEC = "CODEC(Gorilla, ZSTD)"
INT_CODEC = "CODEC(T64, ZSTD)"


@dataclass(frozen=True)
class Column:
    name: str
    ch_type: str
    codec: str = ""

    @property
    def arrow(self) -> pa.DataType:
        t = self.ch_type.removeprefix("Nullable(").removesuffix(")") if self.ch_type.startswith("Nullable") \
            else self.ch_type
        if t.startswith("LowCardinality") or t == "String":
            return pa.string()
        if t.startswith("DateTime64"):
            return pa.timestamp("ms", tz="UTC")
        return {"Float64": pa.float64(), "Int64": pa.int64(), "UInt64": pa.uint64(), "UInt8": pa.uint8(),
                "Int16": pa.int16(), "Bool": pa.bool_()}[t]


@dataclass(frozen=True)
class TableSpec:
    name: str
    columns: tuple[Column, ...]
    order_by: tuple[str, ...]
    ttl_days: int | None = None

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


def _f(name: str) -> Column:
    return Column(name, "Float64", F64_CODEC)


_KEY = (Column("hh_symbol", "LowCardinality(String)"), Column("source", "LowCardinality(String)"),
        Column("source_symbol", "LowCardinality(String)"), Column("ts", "DateTime64(3, 'UTC')", TS_CODEC))
_VERSION = Column("version", "UInt8")

TABLES: dict[str, TableSpec] = {
    "bars_1m": TableSpec("bars_1m", _KEY + (
        _f("open"), _f("high"), _f("low"), _f("close"), _f("volume"), _f("quote_volume"),
        Column("trades", "Int64", INT_CODEC), _f("taker_buy_volume"), _f("taker_buy_quote_volume"),
        Column("halted", "Bool"), Column("zero_vol", "Bool"), Column("synthetic", "Bool"), _VERSION,
    ), ("hh_symbol", "source", "ts")),
    "mark_1m": TableSpec("mark_1m", _KEY + (_f("open"), _f("high"), _f("low"), _f("close"), _VERSION),
                         ("hh_symbol", "source", "ts")),
    "funding": TableSpec("funding", _KEY + (
        _f("rate"), Column("mark_price", "Nullable(Float64)", "CODEC(ZSTD)"),
        Column("interval_hours", "Nullable(Int16)"), _VERSION,
    ), ("hh_symbol", "source", "ts")),
    "metrics": TableSpec("metrics", _KEY + tuple(_f(c) for c in store.METRICS_COLS if c != "ts") + (_VERSION,),
                         ("hh_symbol", "source", "ts")),
    "book_depth": TableSpec("book_depth", _KEY + (_f("percentage"), _f("depth"), _f("notional"), _VERSION),
                            ("hh_symbol", "source", "ts", "percentage")),
    "agg_trades": TableSpec("agg_trades", _KEY + (
        Column("agg_id", "Int64", "CODEC(Delta, ZSTD)"), _f("price"), _f("qty"),
        Column("first_id", "Int64", "CODEC(Delta, ZSTD)"), Column("last_id", "Int64", "CODEC(Delta, ZSTD)"),
        Column("is_buyer_maker", "Bool"), _VERSION,
    ), ("hh_symbol", "source", "ts", "agg_id"), ttl_days=AGG_TRADES_TTL_DAYS),
}


def _check_ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise ValueError(f"недопустимое имя {name!r}")
    return name


def create_table_sql(table: str, database: str = DEFAULT_DATABASE, *, ttl_days: int | None = None) -> str:
    spec = TABLES[table]
    db = _check_ident(database)
    cols = ",\n".join(f"    `{c.name}` {c.ch_type}{(' ' + c.codec) if c.codec else ''}" for c in spec.columns)
    ttl = ttl_days if ttl_days is not None else spec.ttl_days
    lines = [
        f"CREATE TABLE IF NOT EXISTS {db}.{spec.name}",
        "(",
        cols,
        ")",
        "ENGINE = ReplacingMergeTree(version)",
        "PARTITION BY toYYYYMM(ts)",
        f"ORDER BY ({', '.join(spec.order_by)})",
    ]
    if ttl:
        lines.append(f"TTL toDateTime(ts) + INTERVAL {int(ttl)} DAY DELETE")
    lines.append("SETTINGS index_granularity = 8192;")
    return "\n".join(lines)


def schema_sql(database: str = DEFAULT_DATABASE, *, ttl_days: int | None = None) -> str:
    """Полный DDL (deploy/clickhouse/schema.sql генерируется отсюда)."""
    head = [
        "-- ClickHouse DDL (генерируется quant.data.clickhouse.schema_sql, не править руками).",
        "-- ReplacingMergeTree(version): архив 3 > REST 2 > WS 1. Без materialized views (задвоение объёма).",
        f"CREATE DATABASE IF NOT EXISTS {_check_ident(database)};",
    ]
    body = [create_table_sql(t, database, ttl_days=ttl_days if TABLES[t].ttl_days else None) for t in TABLES]
    return "\n\n".join(["\n".join(head)] + body) + "\n"


# ---------------------------------------------------------------- norm → кадры для вставки

def _norm_files(table: str, hh: str, data_dir: Path | None) -> list[Path]:
    if table == "bars_1m":
        return store.period_files(store.bars_dir(hh, data_dir)) + store.period_files(store.live_dir(hh, data_dir))
    if table == "mark_1m":
        return store.period_files(store.mark_dir(hh, data_dir))
    if table == "funding":
        p = store.funding_path(hh, data_dir)
        return [p] if p.exists() else []
    if table == "metrics":
        p = store.metrics_path(hh, data_dir)
        return [p] if p.exists() else []
    if table == "book_depth":
        return store.period_files(store.book_depth_dir(hh, data_dir))
    if table == "agg_trades":
        return store.period_files(store.agg_trades_dir(hh, data_dir))
    raise KeyError(table)


def to_arrow(table: str, hh: str, df: pl.DataFrame, *, source: str = SOURCE_BINANCE_UM,
             source_symbol: str | None = None) -> pa.Table:
    """norm-кадр → Arrow-таблица в порядке и типах колонок ClickHouse."""
    spec = TABLES[table]
    if source_symbol is None:
        inst = by_symbol().get(hh)
        source_symbol = (inst.source_symbol(source) if inst else None) or hh
    consts = {"hh_symbol": hh, "source": source, "source_symbol": source_symbol}
    exprs = []
    for c in spec.columns:
        if c.name == "source" and "source" in df.columns:
            exprs.append(pl.col("source").fill_null(source).alias("source"))
        elif c.name in consts:
            exprs.append(pl.lit(consts[c.name]).alias(c.name))
        elif c.name == "version" and "version" not in df.columns:
            exprs.append(pl.lit(VERSION_ARCHIVE).alias("version"))
        elif c.name in df.columns:
            exprs.append(pl.col(c.name))
        else:
            exprs.append(pl.lit(None).alias(c.name))
    out = df.select(exprs)
    target = pa.schema([pa.field(c.name, c.arrow, nullable=True) for c in spec.columns])
    return out.to_arrow().cast(target)


class ClickHouseClient(Protocol):  # то, что нужно от clickhouse_connect.Client
    def command(self, cmd: str, *args: Any, **kw: Any) -> Any: ...
    def insert_arrow(self, table: str, arrow_table: Any, database: str | None = None, **kw: Any) -> Any: ...
    def query(self, query: str, *args: Any, **kw: Any) -> Any: ...


def connect(url: str | None = None):
    """clickhouse_connect-клиент по DSN (http://user:pass@host:8123/db)."""
    import clickhouse_connect

    from quant.config import get_settings

    dsn = url or get_settings().clickhouse_url
    if not dsn:
        raise ValueError("не задан URL ClickHouse (--url или QUANT_CLICKHOUSE_URL)")
    return clickhouse_connect.get_client(dsn=dsn)


class ClickHouseLoader:
    def __init__(self, client: ClickHouseClient, *, database: str = DEFAULT_DATABASE,
                 data_dir: Path | None = None, ttl_days: int = AGG_TRADES_TTL_DAYS):
        self.client = client
        self.db = _check_ident(database)
        self.data_dir = data_dir
        self.ttl_days = ttl_days

    def create_schema(self, *, drop: bool = False, tables: list[str] | None = None) -> None:
        self.client.command(f"CREATE DATABASE IF NOT EXISTS {self.db}")
        for t in tables or list(TABLES):
            if drop:
                self.client.command(f"DROP TABLE IF EXISTS {self.db}.{t}")
            self.client.command(create_table_sql(t, self.db, ttl_days=self.ttl_days if TABLES[t].ttl_days else None)
                                .rstrip(";"))

    def _ttl_cutoff(self, table: str):
        return now_utc() - timedelta(days=self.ttl_days) if TABLES[table].ttl_days else None

    def load_symbol(self, hh: str, tables: list[str] | None = None) -> dict[str, int]:
        """Залить norm-файлы символа (по файлу за вставку). Дубли схлопнет ReplacingMergeTree."""
        counts: dict[str, int] = {}
        for t in tables or list(TABLES):
            n = 0
            cutoff = self._ttl_cutoff(t)
            for f in _norm_files(t, hh, self.data_dir):
                df = pl.read_parquet(f)
                if cutoff is not None:
                    df = df.filter(pl.col("ts") >= cutoff)
                if df.is_empty():
                    continue
                self.client.insert_arrow(t, to_arrow(t, hh, df), database=self.db)
                n += df.height
            counts[t] = n
        return counts

    def rebuild(self, symbols: list[str] | None = None, tables: list[str] | None = None, *,
                optimize: bool = True) -> dict:
        """Полная пересборка из Parquet: DROP + CREATE + заливка + OPTIMIZE FINAL + сверка счётчиков."""
        tables = tables or list(TABLES)
        symbols = symbols or store.available_symbols(data_dir=self.data_dir)
        self.create_schema(drop=True, tables=tables)
        loaded = {hh: self.load_symbol(hh, tables) for hh in symbols}
        if optimize:
            for t in tables:
                self.client.command(f"OPTIMIZE TABLE {self.db}.{t} FINAL")
        return {"loaded": loaded, "mismatches": self.verify(symbols, tables)}

    def expected_rows(self, table: str, hh: str) -> int:
        """Уникальные строки в Parquet (после дедупа по ключу ORDER BY)."""
        files = _norm_files(table, hh, self.data_dir)
        if not files:
            return 0
        key = [k for k in TABLES[table].order_by if k not in ("hh_symbol", "source")]
        lf = pl.concat([pl.scan_parquet(f).select(key) for f in files], how="vertical")
        cutoff = self._ttl_cutoff(table)
        if cutoff is not None:
            lf = lf.filter(pl.col("ts") >= cutoff)
        return int(lf.unique().select(pl.len()).collect().item())

    def verify(self, symbols: list[str], tables: list[str] | None = None) -> list[dict]:
        """Сравнить число строк ClickHouse (FINAL) с Parquet по каждому символу и таблице."""
        out = []
        for t in tables or list(TABLES):
            res = self.client.query(f"SELECT hh_symbol, count() FROM {self.db}.{t} FINAL GROUP BY hh_symbol")
            got = {r[0]: int(r[1]) for r in res.result_rows}
            for hh in symbols:
                exp = self.expected_rows(t, hh)
                if got.get(hh, 0) != exp:
                    out.append({"table": t, "hh_symbol": hh, "parquet": exp, "clickhouse": got.get(hh, 0)})
        return out


def write_schema_file(path: Path, database: str = DEFAULT_DATABASE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(schema_sql(database), encoding="utf-8", newline="\n")
