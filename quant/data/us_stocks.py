"""Фаза 8: данные рынка US Stocks (отдельная платформа HashHedge, market=us_stocks).

Источник — Alpaca Market Data API v2 (ключи QUANT_ALPACA_KEY / QUANT_ALPACA_SECRET; бесплатный фид IEX,
полный SIP — по подписке). Бары 1m с поправкой на сплиты и дивиденды (adjustment=all), только регулярная
сессия NYSE (календарь XNYS учитывает праздники и переход на зимнее время). Mark price у акций нет —
equity считается по last (`equity_price_source` для us_stocks = last).
Хранилище: data_dir/norm/us_bars_1m/<TICKER>/<YYYY>.parquet (схема BARS_1M).
"""

from __future__ import annotations

import argparse
import logging
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import numpy as np
import polars as pl

from quant.config import get_settings
from quant.schemas import BARS_1M, conform
from quant.sessions import open_mask
from quant.timeutil import now_utc, parse_dt

log = logging.getLogger(__name__)
BASE = "https://data.alpaca.markets/v2"


class AlpacaClient:
    def __init__(self, key: str, secret: str, *, feed: str = "iex", transport: httpx.BaseTransport | None = None):
        if not key or not secret:
            raise RuntimeError("нужны QUANT_ALPACA_KEY и QUANT_ALPACA_SECRET")
        self.feed = feed
        self.http = httpx.Client(base_url=BASE, timeout=30, transport=transport,
                                 headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    def bars(self, symbol: str, start: datetime, end: datetime, *, adjustment: str = "all") -> pl.DataFrame:
        rows, token = [], None
        while True:
            params = {"timeframe": "1Min", "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                      "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"), "adjustment": adjustment, "feed": self.feed,
                      "limit": 10_000}
            if token:
                params["page_token"] = token
            for attempt in range(5):
                r = self.http.get(f"/stocks/{symbol}/bars", params=params)
                if r.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                r.raise_for_status()
                break
            data = r.json()
            rows.extend(data.get("bars") or [])
            token = data.get("next_page_token")
            if not token:
                break
        return to_bars(rows)


def to_bars(rows: list[dict]) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=BARS_1M)
    df = pl.DataFrame(rows).rename({"t": "ts", "o": "open", "h": "high", "l": "low", "c": "close", "v": "volume",
                                    "n": "trades"})
    df = df.with_columns(
        pl.col("ts").str.to_datetime(time_zone="UTC").dt.cast_time_unit("ms"),
        (pl.col("vw") * pl.col("volume")).alias("quote_volume") if "vw" in df.columns else pl.lit(None).alias("quote_volume"),
        (pl.col("volume") == 0).alias("zero_vol"),
        pl.lit(False).alias("halted"), pl.lit(False).alias("synthetic"),
        pl.lit(3, dtype=pl.Int8).alias("version"), pl.lit("alpaca").alias("source"))
    ms = df["ts"].dt.epoch("ms").to_numpy().astype(np.int64)
    df = df.filter(pl.Series(open_mask("EQUITY", ms)))          # только регулярная сессия NYSE
    return conform(df, BARS_1M, extra=False).sort("ts")


def us_dir(ticker: str, data_dir: Path | None = None) -> Path:
    return (data_dir or get_settings().data_dir) / "norm" / "us_bars_1m" / ticker.upper()


def write_bars(ticker: str, df: pl.DataFrame, data_dir: Path | None = None) -> None:
    d = us_dir(ticker, data_dir)
    d.mkdir(parents=True, exist_ok=True)
    for (year,), part in df.group_by(pl.col("ts").dt.year()):
        path = d / f"{year}.parquet"
        if path.exists():
            part = pl.concat([pl.read_parquet(path), part]).unique(subset=["ts"], keep="last").sort("ts")
        part.write_parquet(path)


def load_us_bars(ticker: str, start: datetime | None = None, end: datetime | None = None,
                 data_dir: Path | None = None) -> pl.DataFrame:
    d = us_dir(ticker, data_dir)
    files = sorted(d.glob("*.parquet"))
    if not files:
        return pl.DataFrame(schema=BARS_1M)
    lf = pl.scan_parquet(files)
    if start:
        lf = lf.filter(pl.col("ts") >= start)
    if end:
        lf = lf.filter(pl.col("ts") < end)
    return lf.collect().sort("ts")


def backfill(tickers: list[str], start: datetime, end: datetime, *, feed: str = "iex",
             client: AlpacaClient | None = None) -> dict[str, int]:
    s = get_settings()
    client = client or AlpacaClient(getattr(s, "alpaca_key", ""), getattr(s, "alpaca_secret", ""), feed=feed)
    out = {}
    for t in tickers:
        cur, total = start, 0
        while cur < end:
            nxt = min(end, cur + timedelta(days=30))
            df = client.bars(t, cur, nxt)
            if df.height:
                write_bars(t, df)
                total += df.height
            cur = nxt
        out[t] = total
        log.info("%s: %d баров", t, total)
    return out


def _cli(args: argparse.Namespace) -> int:
    tickers = [x.strip().upper() for x in args.symbols.split(",") if x.strip()]
    res = backfill(tickers, parse_dt(args.start), parse_dt(args.end) if args.end else now_utc(), feed=args.feed)
    for t, n in res.items():
        print(f"{t}: {n} баров")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("us", help="фаза 8: данные US Stocks (Alpaca, нужны ключи QUANT_ALPACA_KEY/SECRET)")
    s = p.add_subparsers(dest="cmd", required=True)
    b = s.add_parser("backfill")
    b.add_argument("--symbols", required=True)
    b.add_argument("--start", required=True)
    b.add_argument("--end")
    b.add_argument("--feed", default="iex", choices=["iex", "sip"])
    b.set_defaults(func=_cli)
