"""Разбор CSV архива data.binance.vision и ответов REST в raw-кадры (3.2.4).

Raw = как пришло, но типизировано и без потери точности:
- цены/объёмы — Decimal(38, 18) (строки на входе, float только в norm-слое);
- время — Datetime("us", "UTC"): futures klines приходят в мс, spot с 2025-01-01 — в мкс
  (детект по величине числа), bookDepth/metrics — строкой даты;
- CSV бывают с заголовком и без (старые файлы — без): заголовок определяется по первому полю.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

import polars as pl

DEC = pl.Decimal(38, 18)
TIME = pl.Datetime("us", "UTC")

# Колонки по видам файлов (порядок как в CSV) и их типы.
KLINES = {
    "open_time": "time_int", "open": "dec", "high": "dec", "low": "dec", "close": "dec",
    "volume": "dec", "close_time": "time_int", "quote_volume": "dec", "count": "int",
    "taker_buy_volume": "dec", "taker_buy_quote_volume": "dec", "ignore": "str",
}
FUNDING = {"calc_time": "time_int", "funding_interval_hours": "int", "last_funding_rate": "dec"}
METRICS = {
    "create_time": "time_str", "symbol": "str", "sum_open_interest": "dec",
    "sum_open_interest_value": "dec", "count_toptrader_long_short_ratio": "dec",
    "sum_toptrader_long_short_ratio": "dec", "count_long_short_ratio": "dec",
    "sum_taker_long_short_vol_ratio": "dec",
}
BOOK_DEPTH = {"timestamp": "time_str", "percentage": "dec", "depth": "dec", "notional": "dec"}
AGG_TRADES = {
    "agg_trade_id": "int", "price": "dec", "quantity": "dec", "first_trade_id": "int",
    "last_trade_id": "int", "transact_time": "time_int", "is_buyer_maker": "bool",
}
REST_FUNDING = {"symbol": "str", "fundingTime": "time_int", "fundingRate": "dec", "markPrice": "dec"}

KINDS: dict[str, dict[str, str]] = {
    "klines": KLINES, "funding": FUNDING, "metrics": METRICS, "book_depth": BOOK_DEPTH,
    "agg_trades": AGG_TRADES, "rest_funding": REST_FUNDING,
}

_DTYPES = {"time_int": TIME, "time_str": TIME, "dec": DEC, "int": pl.Int64, "bool": pl.Boolean, "str": pl.Utf8}

# Время-ключ каждого вида (для сортировки/обрезки).
TIME_COL = {"klines": "open_time", "funding": "calc_time", "metrics": "create_time",
            "book_depth": "timestamp", "agg_trades": "transact_time", "rest_funding": "fundingTime"}


def raw_schema(kind: str) -> dict[str, pl.DataType]:
    return {c: _DTYPES[t] for c, t in KINDS[kind].items()}


def empty_raw(kind: str) -> pl.DataFrame:
    return pl.DataFrame(schema=raw_schema(kind))


def has_header(first_line: str) -> bool:
    """Заголовок, если первое поле начинается с буквы/подчёркивания (данные — с цифры или '-')."""
    first = first_line.lstrip("﻿").split(",", 1)[0].strip().strip('"')
    return bool(re.match(r"^[A-Za-z_]", first))


def int_time_to_us(expr: pl.Expr) -> pl.Expr:
    """Целое время в мкс: ≥1e15 — уже мкс (spot с 2025), ≥1e12 — мс, иначе секунды."""
    v = expr.cast(pl.Int64)
    us = (pl.when(v >= 10**15).then(v)
          .when(v >= 10**12).then(v * 1_000)
          .otherwise(v * 1_000_000))
    return us.cast(pl.Datetime("us")).dt.replace_time_zone("UTC")


def str_time_to_us(expr: pl.Expr) -> pl.Expr:
    return (expr.str.strip_chars()
            .str.to_datetime(time_unit="us", time_zone="UTC", strict=True))


def _cast(col: str, typ: str) -> pl.Expr:
    raw = pl.col(col).str.strip_chars()
    # пустое поле → null (в старых metrics ratio-колонки бывают пустыми)
    c = pl.when(raw.str.len_bytes() == 0).then(None).otherwise(raw)
    if typ == "time_int":
        return int_time_to_us(c).alias(col)
    if typ == "time_str":
        return str_time_to_us(c).alias(col)
    if typ == "dec":
        return c.cast(DEC, strict=True).alias(col)
    if typ == "int":
        return c.cast(pl.Int64, strict=True).alias(col)
    if typ == "bool":
        return (c.str.to_lowercase() == "true").alias(col)
    return pl.col(col).alias(col)


def _typed(lf: pl.LazyFrame, kind: str, header: list[str] | None) -> pl.LazyFrame:
    spec = KINDS[kind]
    names = list(spec)
    got = header if header is not None else lf.collect_schema().names()
    if len(got) != len(names):
        raise ValueError(f"{kind}: {len(got)} колонок вместо {len(names)}: {got}")
    lf = lf.rename(dict(zip(got, names)))
    return lf.select([_cast(c, t) for c, t in spec.items()])


def parse_bytes(data: bytes, kind: str) -> pl.DataFrame:
    """CSV из памяти → типизированный raw-кадр."""
    text_head = data[:512].decode("utf-8", errors="replace")
    if not text_head.strip():
        return empty_raw(kind)
    first_line = text_head.splitlines()[0]
    hdr = has_header(first_line)
    names = list(KINDS[kind])
    df = pl.read_csv(
        io.BytesIO(data), has_header=hdr, infer_schema=False,
        new_columns=None if hdr else names, truncate_ragged_lines=False,
    )
    if df.width != len(names):
        raise ValueError(f"{kind}: {df.width} колонок вместо {len(names)}")
    return _typed(df.lazy(), kind, df.columns).collect()


def scan_file(path: Path, kind: str) -> pl.LazyFrame:
    """Большой CSV на диске → ленивый типизированный кадр (для sink_parquet без загрузки в память)."""
    with open(path, encoding="utf-8", errors="replace") as f:
        first_line = f.readline()
    names = list(KINDS[kind])
    hdr = has_header(first_line)
    if hdr:
        header = [h.strip().strip('"') for h in first_line.lstrip("﻿").strip().split(",")]
        lf = pl.scan_csv(path, has_header=True, infer_schema=False)
        return _typed(lf, kind, header)
    lf = pl.scan_csv(path, has_header=False, infer_schema=False, new_columns=names)
    return _typed(lf, kind, names)


# ---------------------------------------------------------------- REST → raw

def klines_from_rest(rows: list[list]) -> pl.DataFrame:
    """Ответ /fapi/v1/klines | markPriceKlines (списки строк) → raw-кадр вида klines."""
    if not rows:
        return empty_raw("klines")
    names = list(KLINES)
    cols = {n: [str(r[i]) if i < len(r) else "0" for r in rows] for i, n in enumerate(names)}
    return _typed(pl.DataFrame(cols).lazy(), "klines", names).collect()


def funding_from_rest(rows: list[dict]) -> pl.DataFrame:
    """Ответ /fapi/v1/fundingRate → raw-кадр rest_funding. Пустой markPrice → null."""
    if not rows:
        return empty_raw("rest_funding")
    df = pl.DataFrame({
        "symbol": [r.get("symbol", "") for r in rows],
        "fundingTime": [str(r["fundingTime"]) for r in rows],
        "fundingRate": [str(r["fundingRate"]) for r in rows],
        "markPrice": [(str(r.get("markPrice")) if r.get("markPrice") not in (None, "") else None) for r in rows],
    }, schema={"symbol": pl.Utf8, "fundingTime": pl.Utf8, "fundingRate": pl.Utf8, "markPrice": pl.Utf8})
    return _typed(df.lazy(), "rest_funding", list(REST_FUNDING)).collect()
