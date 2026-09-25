"""raw → norm: нормализация 1000-контрактов, history_start, halted, дедуп по version, funding."""

from __future__ import annotations

from datetime import timedelta

import polars as pl
import pytest

from _datahelpers import kline_lines, write_raw, write_raw_klines
from quant.data import store
from quant.data.aggregate import dedup_1m, resample
from quant.data.build import build_symbol, funding_ts, normalize_klines
from quant.data.flags import add_flags, halted_runs
from quant.schemas import BARS_1M, FUNDING, MARK_1M
from quant.timeutil import to_ms, utc


def test_normalization_1000_contract(tmp_settings):
    """PEPEUSDT ↔ 1000PEPEUSDT: price/1000, qty*1000, notional без изменений (3.1.2)."""
    t0 = utc(2024, 3, 1)
    csv = (f"{to_ms(t0)},0.0105000,0.0106000,0.0104000,0.0105500,200000,{to_ms(t0) + 59999},2110.0,50,"
           f"80000,844.0,0\n")
    write_raw("klines", "klines_1m", "1000PEPEUSDT", "2024-03", csv)
    write_raw("klines", "markPriceKlines_1m", "1000PEPEUSDT", "2024-03",
              f"{to_ms(t0)},0.0105100,0.0105900,0.0104100,0.0105400,0,{to_ms(t0) + 59999},0,60,0,0,0\n")
    write_raw("funding", "fundingRate", "1000PEPEUSDT", "2024-03",
              f"calc_time,funding_interval_hours,last_funding_rate\n{to_ms(t0) + 8},8,0.0001\n")
    write_raw("book_depth", "bookDepth", "1000PEPEUSDT", "2024-03-01",
              "timestamp,percentage,depth,notional\n2024-03-01 00:00:04,-1.00,5000.5,52.5\n")
    write_raw("agg_trades", "aggTrades", "1000PEPEUSDT", "2024-03",
              f"1,0.0105,10.5,1,2,{to_ms(t0) + 5},true\n")
    write_raw("metrics", "metrics", "1000PEPEUSDT", "2024-03-01",
              "create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,"
              "sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
              "2024-03-01 00:05:00,1000PEPEUSDT,1000000,10500,1.1,1.2,1.3,1.4\n")
    build_symbol("PEPEUSDT")
    b = store.load_bars("PEPEUSDT", with_mark=True)
    assert b.height == 1
    r = b.row(0, named=True)
    assert r["open"] == pytest.approx(0.0000105, rel=1e-12)
    assert r["close"] == pytest.approx(0.00001055, rel=1e-12)
    assert r["volume"] == pytest.approx(200_000_000)
    assert r["taker_buy_volume"] == pytest.approx(80_000_000)
    assert r["quote_volume"] == pytest.approx(2110.0)                      # notional не меняется
    assert r["close"] * r["volume"] == pytest.approx(0.01055 * 200000)     # notional инвариантен
    assert r["m_close"] == pytest.approx(0.0000105400, rel=1e-12)
    f = store.load_funding("PEPEUSDT")
    assert f["mark_price"][0] == pytest.approx(0.0000105100, rel=1e-12)    # фоллбэк — open mark 1m
    bd = store.load_book_depth("PEPEUSDT")
    assert bd["depth"][0] == pytest.approx(5_000_500) and bd["notional"][0] == pytest.approx(52.5)
    at = store.load_agg_trades("PEPEUSDT", utc(2024, 3, 1), utc(2024, 3, 2))
    assert at["price"][0] == pytest.approx(0.0000105) and at["qty"][0] == pytest.approx(10_500)
    mt = store.load_metrics("PEPEUSDT")
    assert mt["sum_open_interest"][0] == pytest.approx(1e9) and mt["sum_open_interest_value"][0] == 10500


def test_history_start_cut_minute_precision(tmp_settings):
    """LITUSDT: history_start 2025-12-23 17:30 — всё раньше не попадает в norm (raw остаётся)."""
    lines = kline_lines(utc(2025, 12, 23, 17, 0), 30, frozen=True, price=0.592) + \
        kline_lines(utc(2025, 12, 23, 17, 30), 30, price=3.7)
    write_raw_klines("LITUSDT", "2025-12", lines)
    write_raw_klines("LITUSDT", "2025-12", lines, dataset="markPriceKlines_1m")
    write_raw("funding", "fundingRate", "LITUSDT", "2025-12",
              f"{to_ms(utc(2025, 12, 23, 16))},8,0.0001\n{to_ms(utc(2025, 12, 23, 17, 45))},8,0.0002\n")
    build_symbol("LITUSDT")
    b = store.load_bars("LITUSDT")
    assert b["ts"].min() == utc(2025, 12, 23, 17, 30)
    assert b.height == 30
    assert store.load_bars("LITUSDT", utc(2025, 1, 1), utc(2025, 12, 23, 17, 30)).is_empty()
    assert store.load_mark("LITUSDT")["ts"].min() == utc(2025, 12, 23, 17, 30)
    assert store.load_funding("LITUSDT")["ts"].to_list() == [utc(2025, 12, 23, 17, 45)]
    assert store.coverage("LITUSDT")["first_ts"] == utc(2025, 12, 23, 17, 30)
    raw = pl.read_parquet(tmp_settings.data_dir / "raw/binance_um/klines_1m/LITUSDT/LITUSDT-klines_1m-2025-12.parquet")
    assert raw.height == 60                                                   # raw как есть


def _bars(ts0, rows):
    """rows: [(o,h,l,c,v)] по минутам с ts0."""
    return pl.DataFrame({
        "ts": [ts0 + timedelta(minutes=i) for i in range(len(rows))],
        "open": [float(r[0]) for r in rows], "high": [float(r[1]) for r in rows], "low": [float(r[2]) for r in rows],
        "close": [float(r[3]) for r in rows], "volume": [float(r[4]) for r in rows],
    }).with_columns(pl.col("ts").cast(pl.Datetime("ms", "UTC")))


def test_halted_detector_runs_and_threshold():
    live = [(1, 2, 0.5, 1.5, 3.0)]
    frozen = [(1.5, 1.5, 1.5, 1.5, 0.0)]
    rows = live + frozen * 60 + live + frozen * 59 + live + [(1.5, 1.5, 1.5, 1.5, 0.0)] + live
    df = add_flags(_bars(utc(2024, 1, 1), rows), halt_min_bars=60)
    assert df["halted"].sum() == 60                     # только серия из 60
    assert df["zero_vol"].sum() == 60 + 59 + 1          # zero_vol — все нулевые минуты
    assert not df["synthetic"].any()
    assert add_flags(_bars(utc(2024, 1, 1), rows), halt_min_bars=59)["halted"].sum() == 119
    runs = halted_runs(_bars(utc(2024, 1, 1), rows), 60)
    assert runs.height == 1 and runs["minutes"][0] == 60 and runs["start_ts"][0] == utc(2024, 1, 1, 0, 1)
    # zero-vol, но O≠C — не заморозка
    moving = [(1, 1.2, 1, 1.1, 0.0)] * 100
    assert add_flags(_bars(utc(2024, 1, 1), moving), 60)["halted"].sum() == 0


def test_halted_run_broken_by_time_gap():
    frozen = [(1.5, 1.5, 1.5, 1.5, 0.0)]
    a = _bars(utc(2024, 1, 1), frozen * 40)
    b = _bars(utc(2024, 1, 1, 1, 0), frozen * 40)          # пропуск 20 минут между сериями
    assert add_flags(pl.concat([a, b]), 60)["halted"].sum() == 0


def test_dedup_version_priority_archive_rest_live(tmp_settings):
    t0 = utc(2024, 5, 1)
    arch = kline_lines(t0, 10, price=100)
    write_raw_klines("BTCUSDT", "2024-05-01", arch[:5] + arch[6:])            # минута 5 отсутствует в архиве
    rest = kline_lines(t0, 10, price=500)                                       # REST с другими ценами
    write_raw_klines("BTCUSDT", "2024-05-01", rest, dataset="rest_klines_1m")
    build_symbol("BTCUSDT", ["bars_1m"])
    b = store.load_bars("BTCUSDT", with_mark=False)
    assert b.height == 10
    assert b.filter(pl.col("version") == 3).height == 9
    r5 = b.filter(pl.col("ts") == t0 + timedelta(minutes=5)).row(0, named=True)
    assert (r5["version"], r5["open"]) == (2, pytest.approx(500 + 5 * 0.1))
    assert b.filter(pl.col("ts") == t0)["open"][0] == pytest.approx(100)        # архив > REST
    # live (version 1) перекрывается архивом, свои минуты добавляет
    live = b.filter(pl.col("ts") >= t0 + timedelta(minutes=8)).with_columns(pl.col("open") * 0 + 1.0)
    extra = live.with_columns(pl.col("ts") + pl.duration(minutes=5))           # минуты 13, 14
    store.write_live_bars("BTCUSDT", pl.concat([live, extra]))
    b2 = store.load_bars("BTCUSDT", with_mark=False)
    assert b2.height == 12
    assert b2.filter(pl.col("ts") == t0 + timedelta(minutes=8))["version"][0] == 3
    assert b2.filter(pl.col("ts") >= t0 + timedelta(minutes=13))["version"].to_list() == [1, 1]
    assert store.load_bars("BTCUSDT", with_mark=False, include_live=False).height == 10


def test_reload_same_period_does_not_change_higher_tf(tmp_settings):
    """Приёмка фазы 2: повторная загрузка того же периода не меняет агрегаты старших ТФ."""
    t0 = utc(2024, 6, 1)
    lines = kline_lines(t0, 3 * 1440, price=50, step=0.01)
    write_raw_klines("ETHUSDT", "2024-06", lines)
    build_symbol("ETHUSDT", ["bars_1m"])
    base = {tf: resample(store.load_bars("ETHUSDT", with_mark=False), tf) for tf in ("5m", "1h", "4h", "1d")}
    # тот же период ещё раз: дневные архивные файлы + REST + live-дубли
    for d in range(3):
        write_raw_klines("ETHUSDT", f"2024-06-0{d + 1}", lines[d * 1440:(d + 1) * 1440])
        write_raw_klines("ETHUSDT", f"2024-06-0{d + 1}", lines[d * 1440:(d + 1) * 1440], dataset="rest_klines_1m")
    build_symbol("ETHUSDT", ["bars_1m"])
    store.write_live_bars("ETHUSDT", store.load_bars("ETHUSDT", with_mark=False))
    again = store.load_bars("ETHUSDT", with_mark=False)
    assert again.height == 3 * 1440
    for tf, df in base.items():
        assert resample(again, tf).equals(df), tf
    # и сама дедупликация: склейка дублей с разными version → одна строка на ts
    dup = pl.concat([again, again.with_columns(pl.lit(1, dtype=pl.Int8).alias("version"))])
    assert dedup_1m(dup).height == again.height


def test_funding_ts_rounding_rest_mark_and_fallback(tmp_settings):
    t = utc(2024, 7, 1, 8)
    write_raw_klines("SOLUSDT", "2024-07", kline_lines(utc(2024, 7, 1, 7, 58), 5, price=140),
                     dataset="markPriceKlines_1m")
    write_raw("funding", "fundingRate", "SOLUSDT", "2024-07",
              f"calc_time,funding_interval_hours,last_funding_rate\n{to_ms(t) + 1},8,0.00010000\n"
              f"{to_ms(t + timedelta(hours=8)) + 800},8,-0.00020000\n")
    from quant.data import parse
    from quant.data.backfill import write_rest_funding
    write_rest_funding("SOLUSDT", parse.funding_from_rest([
        {"symbol": "SOLUSDT", "fundingTime": to_ms(t) + 5, "fundingRate": "0.00010000", "markPrice": "141.25"},
    ]))
    build_symbol("SOLUSDT", ["mark_1m", "funding"])
    f = store.load_funding("SOLUSDT")
    assert f.columns == list(FUNDING)
    assert f["ts"].to_list() == [t, t + timedelta(hours=8)]                  # …001 / …800 → вниз до секунды
    assert f["rate"].to_list() == [pytest.approx(0.0001), pytest.approx(-0.0002)]
    assert f["mark_price"][0] == pytest.approx(141.25)                       # из REST
    assert f["mark_price"][1] is None                                        # нет mark 1m в 16:00
    mk = store.load_mark("SOLUSDT")
    assert mk.columns == list(MARK_1M)
    t08 = mk.filter(pl.col("ts") == t)["open"][0]
    # без REST: фоллбэк на open mark 1m той минуты
    (tmp_settings.data_dir / "raw/binance_um/rest_fundingRate/SOLUSDT/SOLUSDT-rest_fundingRate-2024-07.parquet").unlink()
    build_symbol("SOLUSDT", ["funding"])
    assert store.load_funding("SOLUSDT")["mark_price"][0] == pytest.approx(t08)


def test_funding_ts_expr():
    df = pl.DataFrame({"x": [1727740800001, 1727740800999, 1727740801000]}).with_columns(
        pl.col("x").cast(pl.Datetime("ms")).dt.replace_time_zone("UTC"))
    out = df.select(funding_ts("x"))["ts"].dt.epoch("ms").to_list()
    assert out == [1727740800000, 1727740800000, 1727740801000]


def test_norm_schema_is_contract(tmp_settings):
    write_raw_klines("BTCUSDT", "2024-01", kline_lines(utc(2024, 1, 1), 3))
    build_symbol("BTCUSDT", ["bars_1m"])
    b = store.load_bars("BTCUSDT", with_mark=True)
    assert b.columns == list(BARS_1M) + ["m_open", "m_high", "m_low", "m_close"]
    assert dict(b.select(list(BARS_1M)).schema) == BARS_1M
    assert b["m_open"].null_count() == 3                                     # нет mark → null
    assert (b["source"] == "binance_um").all() and not b["synthetic"].any()


def test_normalize_klines_pure():
    from quant.data import parse
    raw = parse.parse_bytes(b"1577836800000,10,11,9,10.5,2,1577836859999,21,3,1,10.5,0\n", "klines")
    n = normalize_klines(raw, 1000)
    assert n["open"][0] == pytest.approx(0.01) and n["volume"][0] == 2000 and n["quote_volume"][0] == 21
