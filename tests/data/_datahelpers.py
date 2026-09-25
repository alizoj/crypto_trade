"""Хелперы тестов слоя данных: синтетические zip в формате Binance, мок S3/архива, raw-файлы."""

from __future__ import annotations

import hashlib
import io
import zipfile
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

import httpx
import polars as pl

from quant.data import parse
from quant.data.datasets import raw_path
from quant.timeutil import to_ms

KLINES_HEADER = ("open_time,open,high,low,close,volume,close_time,quote_volume,count,"
                 "taker_buy_volume,taker_buy_quote_volume,ignore")
LIST_URL = "https://list.test/bucket"
DL_URL = "https://dl.test"


def make_zip(csv_text: str, name: str = "data.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, csv_text)
    return buf.getvalue()


def kline_lines(start: datetime, n: int, *, price: float = 100.0, step: float = 0.1, volume: float = 2.0,
                frozen: bool = False, unit: str = "ms") -> list[str]:
    """Строки CSV klines по минутам. frozen — volume=0 и O=H=L=C."""
    out = []
    for i in range(n):
        t = to_ms(start) + i * 60_000
        mul = 1000 if unit == "us" else 1
        if frozen:
            o = h = lo = c = price
            v = qv = tb = tbq = 0.0
            cnt = 0
        else:
            o = price + i * step
            c = o + step
            h, lo = max(o, c) + step, min(o, c) - step
            v, cnt = volume, 10
            qv = v * c
            tb, tbq = v / 2, v * c / 2
        out.append(f"{t * mul},{o:.6f},{h:.6f},{lo:.6f},{c:.6f},{v},{(t + 59_999) * mul},{qv},{cnt},{tb},{tbq},0")
    return out


def klines_csv(lines: list[str], header: bool = True) -> str:
    return "\n".join(([KLINES_HEADER] if header else []) + lines) + "\n"


def write_raw(kind: str, dataset: str, source_symbol: str, period: str, csv_text: str, data_dir=None) -> pl.DataFrame:
    df = parse.parse_bytes(csv_text.encode(), kind)
    p = raw_path(dataset, source_symbol, period, data_dir=data_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    df.write_parquet(p)
    return df


def write_raw_klines(source_symbol: str, period: str, lines: list[str], *, dataset: str = "klines_1m",
                     data_dir=None) -> pl.DataFrame:
    return write_raw("klines", dataset, source_symbol, period, klines_csv(lines), data_dir)


def minutes(start: datetime, n: int) -> list[datetime]:
    return [start + timedelta(minutes=i) for i in range(n)]


class FakeArchive:
    """Мок S3-листинга (?prefix/marker, IsTruncated) и data.binance.vision (zip + .CHECKSUM)."""

    def __init__(self, page_size: int = 1000):
        self.page_size = page_size
        self.files: dict[str, bytes] = {}
        self.checksums: dict[str, str] = {}
        self.corrupt: dict[str, int] = {}       # key -> сколько раз отдать испорченные байты
        self.requests: list[str] = []

    def add(self, key: str, data: bytes, *, checksum: str | None = None) -> None:
        self.files[key] = data
        self.checksums[key] = checksum or hashlib.sha256(data).hexdigest()

    def _keys(self) -> list[tuple[str, int]]:
        out = []
        for k, v in self.files.items():
            out.append((k, len(v)))
            out.append((k + ".CHECKSUM", 90))
        return sorted(out)

    def handler(self, request: httpx.Request) -> httpx.Response:
        url = urlparse(str(request.url))
        self.requests.append(str(request.url))
        if url.hostname == "list.test":
            q = parse_qs(url.query)
            prefix, marker = q.get("prefix", [""])[0], q.get("marker", [""])[0]
            keys = [kv for kv in self._keys() if kv[0].startswith(prefix) and kv[0] > marker]
            page, truncated = keys[: self.page_size], len(keys) > self.page_size
            body = "".join(f"<Contents><Key>{escape(k)}</Key><Size>{s}</Size></Contents>" for k, s in page)
            xml = ('<?xml version="1.0" encoding="UTF-8"?>'
                   '<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/">'
                   f"<Prefix>{escape(prefix)}</Prefix><IsTruncated>{str(truncated).lower()}</IsTruncated>"
                   f"{body}</ListBucketResult>")
            return httpx.Response(200, content=xml.encode())
        if url.hostname == "dl.test":
            key = url.path.lstrip("/")
            if key.endswith(".CHECKSUM"):
                k = key.removesuffix(".CHECKSUM")
                if k not in self.checksums:
                    return httpx.Response(404)
                return httpx.Response(200, text=f"{self.checksums[k]}  {k.rsplit('/', 1)[-1]}\n")
            if key not in self.files:
                return httpx.Response(404)
            if self.corrupt.get(key, 0) > 0:
                self.corrupt[key] -= 1
                return httpx.Response(200, content=self.files[key][:-5] + b"XXXXX")
            return httpx.Response(200, content=self.files[key])
        return httpx.Response(404)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    def downloads(self, suffix: str = ".zip") -> list[str]:
        return [u for u in self.requests if "dl.test" in u and u.endswith(suffix)]
