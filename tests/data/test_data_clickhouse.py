"""ClickHouse: генерация DDL, маппинг колонок, rebuild/verify на фейковом клиенте (сервер не нужен)."""

from __future__ import annotations

import re
from pathlib import Path

import polars as pl
import pyarrow as pa

from _datahelpers import kline_lines, write_raw, write_raw_klines
from quant.data.build import build_symbol
from quant.data.clickhouse import TABLES, ClickHouseLoader, create_table_sql, schema_sql, to_arrow
from quant.timeutil import to_ms, utc

ROOT = Path(__file__).resolve().parents[2]


def test_ddl_engine_partition_order_codecs():
    for t, spec in TABLES.items():
        sql = create_table_sql(t, "quant")
        assert f"CREATE TABLE IF NOT EXISTS quant.{t}" in sql
        assert "ENGINE = ReplacingMergeTree(version)" in sql
        assert "PARTITION BY toYYYYMM(ts)" in sql
        assert re.search(r"ORDER BY \(hh_symbol, source, ts[,)]", sql)
        assert "`source_symbol` LowCardinality(String)" in sql
        assert "`ts` DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD)" in sql
        assert "`version` UInt8" in sql
        for line in sql.splitlines():
            if re.search(r"` Float64( |$)", line):
                assert "CODEC(Gorilla, ZSTD)" in line, line
    assert "ORDER BY (hh_symbol, source, ts, agg_id)" in create_table_sql("agg_trades")
    assert "ORDER BY (hh_symbol, source, ts, percentage)" in create_table_sql("book_depth")


def test_ttl_only_for_trades_and_no_materialized_views():
    assert "TTL toDateTime(ts) + INTERVAL 90 DAY DELETE" in create_table_sql("agg_trades")
    assert "TTL toDateTime(ts) + INTERVAL 30 DAY DELETE" in create_table_sql("agg_trades", ttl_days=30)
    for t in TABLES:
        if t != "agg_trades":
            assert "TTL" not in create_table_sql(t)
    full = schema_sql()
    assert "MATERIALIZED VIEW" not in full.upper().replace("MATERIALIZED VIEWS", "")
    assert full.count("CREATE TABLE") == len(TABLES)


def test_schema_file_in_deploy_matches_generator():
    p = ROOT / "deploy" / "clickhouse" / "schema.sql"
    assert p.exists(), "deploy/clickhouse/schema.sql: quant data clickhouse-ddl --out ..."
    assert p.read_text(encoding="utf-8") == schema_sql()


def test_to_arrow_column_mapping(tmp_settings):
    df = pl.DataFrame({
        "ts": [utc(2024, 1, 1)], "open": [1.0], "high": [2.0], "low": [0.5], "close": [1.5], "volume": [3.0],
        "quote_volume": [4.5], "trades": [7], "taker_buy_volume": [1.0], "taker_buy_quote_volume": [1.5],
        "halted": [False], "zero_vol": [False], "synthetic": [False], "version": [3], "source": ["binance_um"],
    }).with_columns(pl.col("ts").cast(pl.Datetime("ms", "UTC")), pl.col("version").cast(pl.Int8))
    t = to_arrow("bars_1m", "PEPEUSDT", df)
    assert t.column_names == TABLES["bars_1m"].column_names
    assert t.column("source_symbol").to_pylist() == ["1000PEPEUSDT"]       # тикер источника
    assert t.column("hh_symbol").to_pylist() == ["PEPEUSDT"]
    assert t.schema.field("ts").type == pa.timestamp("ms", tz="UTC")
    assert t.schema.field("version").type == pa.uint8()
    assert t.schema.field("halted").type == pa.bool_()
    f = pl.DataFrame({"ts": [utc(2024, 1, 1)], "rate": [0.0001], "mark_price": [None], "source": ["binance_um"]},
                     schema_overrides={"mark_price": pl.Float64}).with_columns(pl.col("ts").cast(pl.Datetime("ms", "UTC")))
    ft = to_arrow("funding", "BTCUSDT", f)
    assert ft.column("version").to_pylist() == [3] and ft.column("mark_price").to_pylist() == [None]
    assert ft.column("interval_hours").null_count == 1


class FakeClickHouse:
    """Хранит вставки; на count() FROM t FINAL схлопывает дубли по ключу ORDER BY (как ReplacingMergeTree)."""

    def __init__(self):
        self.commands: list[str] = []
        self.inserts: dict[str, list[pa.Table]] = {}

    def command(self, cmd, *a, **kw):
        self.commands.append(cmd)
        m = re.match(r"DROP TABLE IF EXISTS \w+\.(\w+)", cmd)
        if m:
            self.inserts.pop(m.group(1), None)

    def insert_arrow(self, table, arrow_table, database=None, **kw):
        self.inserts.setdefault(table, []).append(arrow_table)

    def query(self, q, *a, **kw):
        m = re.match(r"SELECT hh_symbol, count\(\) FROM \w+\.(\w+) FINAL GROUP BY hh_symbol", q)
        t = m.group(1)
        parts = self.inserts.get(t, [])
        rows = []
        if parts:
            df = pl.from_arrow(pa.concat_tables(parts))
            key = list(TABLES[t].order_by)
            agg = df.unique(subset=key).group_by("hh_symbol").len()
            rows = [(r[0], r[1]) for r in agg.iter_rows()]
        return type("R", (), {"result_rows": rows})()


def _norm_data():
    t0 = utc(2024, 1, 1)
    write_raw_klines("BTCUSDT", "2024-01", kline_lines(t0, 50))
    write_raw_klines("BTCUSDT", "2024-01", kline_lines(t0, 50), dataset="markPriceKlines_1m")
    write_raw("funding", "fundingRate", "BTCUSDT", "2024-01", f"{to_ms(t0) + 1},8,0.0001\n")
    write_raw_klines("1000PEPEUSDT", "2024-01", kline_lines(t0, 30, price=0.01, step=0.0001))
    build_symbol("BTCUSDT")
    build_symbol("PEPEUSDT")


def test_rebuild_drops_creates_loads_and_verifies(tmp_settings):
    _norm_data()
    ch = FakeClickHouse()
    loader = ClickHouseLoader(ch, database="quant")
    rep = loader.rebuild()
    assert rep["mismatches"] == []
    assert rep["loaded"]["BTCUSDT"]["bars_1m"] == 50 and rep["loaded"]["PEPEUSDT"]["bars_1m"] == 30
    assert rep["loaded"]["BTCUSDT"]["funding"] == 1
    assert ch.commands[0] == "CREATE DATABASE IF NOT EXISTS quant"
    for t in TABLES:
        assert f"DROP TABLE IF EXISTS quant.{t}" in ch.commands
        assert any(c.startswith(f"CREATE TABLE IF NOT EXISTS quant.{t}") for c in ch.commands)
        assert f"OPTIMIZE TABLE quant.{t} FINAL" in ch.commands
    assert not any("MATERIALIZED" in c.upper() for c in ch.commands)
    # повторная загрузка (как второй коллектор/догрузка) — FINAL даёт те же числа
    loader.load_symbol("BTCUSDT")
    assert loader.verify(["BTCUSDT", "PEPEUSDT"]) == []
    # rebuild воспроизводит таблицы: те же строки после повторной пересборки
    before = {t: pl.from_arrow(pa.concat_tables(v)).sort(list(TABLES[t].order_by)) for t, v in ch.inserts.items()}
    loader.rebuild()
    after = {t: pl.from_arrow(pa.concat_tables(v)).sort(list(TABLES[t].order_by)) for t, v in ch.inserts.items()}
    assert before.keys() == after.keys()
    for t in before:
        assert before[t].unique(subset=list(TABLES[t].order_by)).sort(list(TABLES[t].order_by)).equals(after[t])


def test_verify_reports_mismatch(tmp_settings):
    _norm_data()
    ch = FakeClickHouse()
    loader = ClickHouseLoader(ch)
    loader.create_schema(drop=True)
    loader.load_symbol("BTCUSDT", ["bars_1m"])
    mm = loader.verify(["BTCUSDT", "PEPEUSDT"], ["bars_1m"])
    assert mm == [{"table": "bars_1m", "hh_symbol": "PEPEUSDT", "parquet": 30, "clickhouse": 0}]
