"""Разбор CSV архива (3.2.4): заголовок/без, мс/мкс, строковые даты, точность Decimal."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import polars as pl

from _datahelpers import klines_csv, kline_lines
from quant.data import parse
from quant.timeutil import utc


def test_klines_with_and_without_header_are_equal():
    lines = kline_lines(utc(2020, 1, 1), 5)
    a = parse.parse_bytes(klines_csv(lines, header=True).encode(), "klines")
    b = parse.parse_bytes(klines_csv(lines, header=False).encode(), "klines")
    assert a.height == b.height == 5
    assert a.equals(b)
    assert a.schema["open_time"] == pl.Datetime("us", "UTC")
    assert a.schema["open"] == parse.DEC
    assert a["open_time"][0] == datetime(2020, 1, 1, tzinfo=timezone.utc)


def test_spot_microseconds_detected_by_magnitude():
    ms = parse.parse_bytes(klines_csv(kline_lines(utc(2025, 1, 2), 3)).encode(), "klines")
    us = parse.parse_bytes(klines_csv(kline_lines(utc(2025, 1, 2), 3, unit="us")).encode(), "klines")
    assert ms["open_time"].to_list() == us["open_time"].to_list()
    assert ms["close_time"].to_list() == us["close_time"].to_list()
    assert us["close_time"][0] == datetime(2025, 1, 2, 0, 0, 59, 999000, tzinfo=timezone.utc)


def test_decimal_precision_is_lossless():
    csv = "1577836800000,0.0000012345,0.0000012399,0.0000012300,0.0000012388,123456789.123,1577836859999," \
          "152.93456123456789,5,1.5,1.85,0\n"
    df = parse.parse_bytes(csv.encode(), "klines")
    assert df["open"][0] == Decimal("0.0000012345")
    assert df["quote_volume"][0] == Decimal("152.934561234567890000")


def test_has_header_does_not_trigger_on_dates():
    assert parse.has_header("open_time,open,high")
    assert parse.has_header("﻿timestamp,percentage,depth,notional")
    assert not parse.has_header("2026-09-20 00:00:04,-5.00,6584.3,526534240.9")
    assert not parse.has_header("1577836800000,7189.43,7190.52")
    assert not parse.has_header("-1,2,3")


def test_funding_metrics_bookdepth_aggtrades():
    f = parse.parse_bytes(b"calc_time,funding_interval_hours,last_funding_rate\n1785542400001,8,0.00004123\n",
                          "funding")
    assert f["calc_time"][0] == datetime(2026, 8, 1, 0, 0, 0, 1000, tzinfo=timezone.utc)
    assert f["last_funding_rate"][0] == Decimal("0.00004123")
    assert f["funding_interval_hours"][0] == 8

    m = parse.parse_bytes(
        ("create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,"
         "sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
         "2026-09-20 00:30:00,BTCUSDT,107482.0080000000000000,8737599861.8673930000000000,1.09,2.08,0.94,0.81\n"
         ).encode(), "metrics")
    assert m["create_time"][0] == datetime(2026, 9, 20, 0, 30, tzinfo=timezone.utc)
    assert m["sum_open_interest_value"][0] == Decimal("8737599861.867393")

    bd = parse.parse_bytes(b"timestamp,percentage,depth,notional\n2026-09-20 00:00:04,-5.00,6584.303,526534240.9384\n"
                           b"2026-09-20 00:00:04,5.00,1.0,2.0\n", "book_depth")
    assert bd.height == 2
    assert bd["timestamp"][0] == datetime(2026, 9, 20, 0, 0, 4, tzinfo=timezone.utc)
    assert bd["percentage"].to_list() == [Decimal("-5"), Decimal("5")]

    rows = "18374167,7189.43,0.030,25247504,25247504,1577836801481,true\n18374168,7189.42,1.470,25247505,25247506,1577836801481,False\n"
    no_hdr = parse.parse_bytes(rows.encode(), "agg_trades")
    hdr = parse.parse_bytes(("agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker\n"
                             + rows).encode(), "agg_trades")
    assert no_hdr.equals(hdr)
    assert no_hdr["is_buyer_maker"].to_list() == [True, False]
    assert no_hdr["transact_time"][0] == datetime(2020, 1, 1, 0, 0, 1, 481000, tzinfo=timezone.utc)


def test_empty_csv_gives_typed_empty_frame():
    df = parse.parse_bytes(b"", "klines")
    assert df.is_empty() and df.schema == parse.raw_schema("klines")


def test_scan_file_matches_parse_bytes(tmp_path):
    text = klines_csv(kline_lines(utc(2021, 3, 1), 10), header=False)
    p = tmp_path / "x.csv"
    p.write_text(text)
    assert parse.scan_file(p, "klines").collect().equals(parse.parse_bytes(text.encode(), "klines"))


def test_rest_parsers():
    k = parse.klines_from_rest([[1577836800000, "7189.43", "7190.52", "7177.0", "7182.44", "246.092", 1577836859999,
                                 "1767430.16121", 336, "46.630", "334813.19820", "0"]])
    assert k["open"][0] == Decimal("7189.43") and k["count"][0] == 336
    f = parse.funding_from_rest([
        {"symbol": "BTCUSDT", "fundingTime": 1640995200006, "fundingRate": "0.0001", "markPrice": ""},
        {"symbol": "BTCUSDT", "fundingTime": 1789027200007, "fundingRate": "0.00007869", "markPrice": "78071.118"},
    ])
    assert f["markPrice"].to_list() == [None, Decimal("78071.118")]


def test_empty_fields_become_null():
    """Старые metrics: пустые ratio-колонки → null, а не ошибка разбора."""
    csv = ("create_time,symbol,sum_open_interest,sum_open_interest_value,count_toptrader_long_short_ratio,"
           "sum_toptrader_long_short_ratio,count_long_short_ratio,sum_taker_long_short_vol_ratio\n"
           "2022-01-01 00:05:00,BTCUSDT,1.5,2.5,,,,\n")
    df = parse.parse_bytes(csv.encode(), "metrics")
    assert df.row(0)[2:] == (Decimal("1.5"), Decimal("2.5"), None, None, None, None)
