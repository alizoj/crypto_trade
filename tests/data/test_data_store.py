"""API norm-слоя: load_* (фильтры, end исключительно, mark), live-бары, coverage, dataset_hash."""

from __future__ import annotations

import shutil
from datetime import timedelta

import polars as pl
import pytest

from _datahelpers import kline_lines, write_raw_klines
from quant.data import store
from quant.data.build import build_symbol
from quant.schemas import BARS_1M
from quant.timeutil import utc


@pytest.fixture
def btc(tmp_settings):
    t0 = utc(2023, 12, 31, 23, 0)
    lines = kline_lines(t0, 120, price=40000)          # через границу года → два файла norm
    write_raw_klines("BTCUSDT", "2023-12", lines[:60])
    write_raw_klines("BTCUSDT", "2024-01", lines[60:])
    write_raw_klines("BTCUSDT", "2024-01", lines[70:100], dataset="markPriceKlines_1m")
    build_symbol("BTCUSDT", ["bars_1m", "mark_1m"])
    return t0


def test_year_files_and_range_filters(btc):
    t0 = btc
    assert sorted(p.name for p in store.bars_dir("BTCUSDT").glob("*.parquet")) == ["2023.parquet", "2024.parquet"]
    all_ = store.load_bars("BTCUSDT")
    assert all_.height == 120 and all_["ts"].is_sorted()
    part = store.load_bars("BTCUSDT", t0 + timedelta(minutes=50), t0 + timedelta(minutes=70))
    assert part.height == 20                                                     # end — исключительно
    assert part["ts"].max() == t0 + timedelta(minutes=69)
    assert store.load_bars("BTCUSDT", "2024-01-01", "2024-01-01T00:10").height == 10   # строки тоже можно


def test_with_mark_left_join(btc):
    b = store.load_bars("BTCUSDT", with_mark=True)
    assert b.columns[-4:] == ["m_open", "m_high", "m_low", "m_close"]
    assert b["m_close"].null_count() == 90                                      # mark только на 30 минутах
    assert b.height == 120
    nb = store.load_bars("BTCUSDT", with_mark=False)
    assert nb.columns == list(BARS_1M)


def test_write_live_bars_replace_and_overlay(btc):
    t0 = btc
    live_ts = [t0 + timedelta(minutes=120 + i) for i in range(3)]
    df = pl.DataFrame({"ts": live_ts, "open": [1.0] * 3, "high": [1.0] * 3, "low": [1.0] * 3,
                       "close": [1.0] * 3, "volume": [0.0, 1.0, 2.0]}).with_columns(
        pl.col("ts").cast(pl.Datetime("ms", "UTC")))
    store.write_live_bars("BTCUSDT", df)
    store.write_live_bars("BTCUSDT", df.head(1).with_columns(pl.lit(9.0).alias("close")))   # тот же ts — замена
    live = pl.read_parquet(store.live_dir("BTCUSDT") / "2024-01-01.parquet")
    assert live.height == 3 and (live["version"] == 1).all()
    assert live.sort("ts")["close"][0] == 9.0 and live.sort("ts")["zero_vol"][0]
    b = store.load_bars("BTCUSDT", with_mark=False)
    assert b.height == 123 and b["version"].to_list()[-3:] == [1, 1, 1]
    cov = store.coverage("BTCUSDT")
    assert cov["live_rows"] == 3 and cov["rows"] == 120


def test_live_before_history_start_dropped(tmp_settings):
    df = pl.DataFrame({"ts": [utc(2025, 12, 23, 17, 29), utc(2025, 12, 23, 17, 30)], "open": [1.0, 1.0],
                       "high": [1.0, 1.0], "low": [1.0, 1.0], "close": [1.0, 1.0], "volume": [1.0, 1.0]}
                      ).with_columns(pl.col("ts").cast(pl.Datetime("ms", "UTC")))
    store.write_live_bars("LITUSDT", df)
    assert store.load_bars("LITUSDT", with_mark=False)["ts"].to_list() == [utc(2025, 12, 23, 17, 30)]


def test_coverage_counts_gaps(tmp_settings):
    t0 = utc(2024, 2, 1)
    lines = kline_lines(t0, 100)
    write_raw_klines("ETHUSDT", "2024-02", lines[:40] + lines[45:])
    build_symbol("ETHUSDT", ["bars_1m"])
    c = store.coverage("ETHUSDT")
    assert (c["rows"], c["gaps_min"], c["first_ts"], c["last_ts"]) == (95, 5, t0, t0 + timedelta(minutes=99))
    assert store.available_symbols() == ["ETHUSDT"]


def test_dataset_hash_deterministic_and_content_based(btc, tmp_settings, tmp_path, monkeypatch):
    h1 = store.dataset_hash(["BTCUSDT"])
    assert h1 == store.dataset_hash(["BTCUSDT"]) == store.dataset_hash(["BTCUSDT", "BTCUSDT"])
    # пересборка тем же содержимым → тот же хэш (не зависит от mtime)
    for p in store.bars_dir("BTCUSDT").glob("*.parquet"):
        p.unlink()
    build_symbol("BTCUSDT", ["bars_1m", "mark_1m"])
    assert store.dataset_hash(["BTCUSDT"]) == h1
    # диапазон влияет, но только используемыми строками
    r1 = store.dataset_hash(["BTCUSDT"], utc(2024, 1, 1), utc(2024, 1, 1, 0, 30))
    assert r1 != h1
    # копия data_dir в другое место → тот же хэш (пути относительные)
    other = tmp_path / "copy"
    shutil.copytree(tmp_settings.data_dir, other)
    assert store.dataset_hash(["BTCUSDT"], data_dir=other) == h1
    # изменение строки вне диапазона не меняет хэш диапазона, внутри — меняет
    y24 = store.bars_dir("BTCUSDT") / "2024.parquet"
    df = pl.read_parquet(y24)
    store.write_norm(df.with_columns(pl.when(pl.col("ts") == df["ts"].max()).then(1.0).otherwise(pl.col("close"))
                                     .alias("close")), y24)
    assert store.dataset_hash(["BTCUSDT"], utc(2024, 1, 1), utc(2024, 1, 1, 0, 30)) == r1
    assert store.dataset_hash(["BTCUSDT"]) != h1


def test_frame_digest_stable():
    df = pl.DataFrame({"ts": [utc(2024, 1, 1)], "x": [1.5], "s": ["a"], "b": [True], "n": [None]},
                      schema_overrides={"n": pl.Float64})
    assert store.frame_digest(df) == store.frame_digest(df.clone())
    assert store.frame_digest(df) != store.frame_digest(df.with_columns(pl.lit(1.6).alias("x")))
    assert store.frame_digest(df) != store.frame_digest(df.with_columns(pl.lit(0.0).alias("n")))


def test_empty_symbol_returns_schema(tmp_settings):
    b = store.load_bars("SOLUSDT")
    assert b.is_empty() and b.columns == list(BARS_1M) + ["m_open", "m_high", "m_low", "m_close"]
    assert store.load_funding("SOLUSDT").is_empty()
    assert store.load_agg_trades("SOLUSDT", utc(2024, 1, 1), utc(2024, 1, 2)).is_empty()
    assert store.coverage("SOLUSDT")["rows"] == 0
