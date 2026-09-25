-- ClickHouse DDL (генерируется quant.data.clickhouse.schema_sql, не править руками).
-- ReplacingMergeTree(version): архив 3 > REST 2 > WS 1. Без materialized views (задвоение объёма).
CREATE DATABASE IF NOT EXISTS quant;

CREATE TABLE IF NOT EXISTS quant.bars_1m
(
    `hh_symbol` LowCardinality(String),
    `source` LowCardinality(String),
    `source_symbol` LowCardinality(String),
    `ts` DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD),
    `open` Float64 CODEC(Gorilla, ZSTD),
    `high` Float64 CODEC(Gorilla, ZSTD),
    `low` Float64 CODEC(Gorilla, ZSTD),
    `close` Float64 CODEC(Gorilla, ZSTD),
    `volume` Float64 CODEC(Gorilla, ZSTD),
    `quote_volume` Float64 CODEC(Gorilla, ZSTD),
    `trades` Int64 CODEC(T64, ZSTD),
    `taker_buy_volume` Float64 CODEC(Gorilla, ZSTD),
    `taker_buy_quote_volume` Float64 CODEC(Gorilla, ZSTD),
    `halted` Bool,
    `zero_vol` Bool,
    `synthetic` Bool,
    `version` UInt8
)
ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(ts)
ORDER BY (hh_symbol, source, ts)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS quant.mark_1m
(
    `hh_symbol` LowCardinality(String),
    `source` LowCardinality(String),
    `source_symbol` LowCardinality(String),
    `ts` DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD),
    `open` Float64 CODEC(Gorilla, ZSTD),
    `high` Float64 CODEC(Gorilla, ZSTD),
    `low` Float64 CODEC(Gorilla, ZSTD),
    `close` Float64 CODEC(Gorilla, ZSTD),
    `version` UInt8
)
ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(ts)
ORDER BY (hh_symbol, source, ts)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS quant.funding
(
    `hh_symbol` LowCardinality(String),
    `source` LowCardinality(String),
    `source_symbol` LowCardinality(String),
    `ts` DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD),
    `rate` Float64 CODEC(Gorilla, ZSTD),
    `mark_price` Nullable(Float64) CODEC(ZSTD),
    `interval_hours` Nullable(Int16),
    `version` UInt8
)
ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(ts)
ORDER BY (hh_symbol, source, ts)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS quant.metrics
(
    `hh_symbol` LowCardinality(String),
    `source` LowCardinality(String),
    `source_symbol` LowCardinality(String),
    `ts` DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD),
    `sum_open_interest` Float64 CODEC(Gorilla, ZSTD),
    `sum_open_interest_value` Float64 CODEC(Gorilla, ZSTD),
    `count_toptrader_long_short_ratio` Float64 CODEC(Gorilla, ZSTD),
    `sum_toptrader_long_short_ratio` Float64 CODEC(Gorilla, ZSTD),
    `count_long_short_ratio` Float64 CODEC(Gorilla, ZSTD),
    `sum_taker_long_short_vol_ratio` Float64 CODEC(Gorilla, ZSTD),
    `version` UInt8
)
ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(ts)
ORDER BY (hh_symbol, source, ts)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS quant.book_depth
(
    `hh_symbol` LowCardinality(String),
    `source` LowCardinality(String),
    `source_symbol` LowCardinality(String),
    `ts` DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD),
    `percentage` Float64 CODEC(Gorilla, ZSTD),
    `depth` Float64 CODEC(Gorilla, ZSTD),
    `notional` Float64 CODEC(Gorilla, ZSTD),
    `version` UInt8
)
ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(ts)
ORDER BY (hh_symbol, source, ts, percentage)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS quant.agg_trades
(
    `hh_symbol` LowCardinality(String),
    `source` LowCardinality(String),
    `source_symbol` LowCardinality(String),
    `ts` DateTime64(3, 'UTC') CODEC(DoubleDelta, ZSTD),
    `agg_id` Int64 CODEC(Delta, ZSTD),
    `price` Float64 CODEC(Gorilla, ZSTD),
    `qty` Float64 CODEC(Gorilla, ZSTD),
    `first_id` Int64 CODEC(Delta, ZSTD),
    `last_id` Int64 CODEC(Delta, ZSTD),
    `is_buyer_maker` Bool,
    `version` UInt8
)
ENGINE = ReplacingMergeTree(version)
PARTITION BY toYYYYMM(ts)
ORDER BY (hh_symbol, source, ts, agg_id)
TTL toDateTime(ts) + INTERVAL 90 DAY DELETE
SETTINGS index_granularity = 8192;
