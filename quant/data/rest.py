"""REST-клиенты Binance USDⓈ-M / spot и Bybit V5 (только публичные эндпоинты).

- Пагинация по времени (klines, fundingRate) и по cursor (Bybit instruments-info).
- Лимиты: Binance — вес запросов (2400/мин, следим за X-MBX-USED-WEIGHT-1M),
  fundingRate/fundingInfo — 500 за 5 мин; Bybit — 600 запросов за 5 с (берём с запасом).
- Ретраи с экспоненциальным backoff на 429/418/5xx и сетевые ошибки; Retry-After учитывается.
Бары из REST при записи получают version=2 (3.2.5) — это делает вызывающий код.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections import deque
from typing import Any

import httpx

log = logging.getLogger(__name__)

BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_SPOT = "https://api.binance.com"
BYBIT_API = "https://api.bybit.com"

MS_MIN = 60_000


class RestError(RuntimeError):
    pass


class RateLimiter:
    """Скользящее окно: не больше max_units за period секунд (потокобезопасно)."""

    def __init__(self, max_units: int, period: float):
        self.max_units = max_units
        self.period = period
        self._events: deque[tuple[float, int]] = deque()
        self._used = 0
        self._lock = threading.Lock()

    def acquire(self, units: int = 1) -> None:
        units = min(units, self.max_units)
        while True:
            with self._lock:
                now = time.monotonic()
                while self._events and now - self._events[0][0] >= self.period:
                    self._used -= self._events.popleft()[1]
                if self._used + units <= self.max_units:
                    self._events.append((now, units))
                    self._used += units
                    return
                wait = self.period - (now - self._events[0][0]) + 0.01
            time.sleep(max(wait, 0.01))


class _Http:
    """Базовый GET JSON с ретраями."""

    def __init__(self, base_url: str, *, timeout: float = 30.0, retries: int = 6,
                 backoff: float = 1.0, transport: httpx.BaseTransport | None = None):
        self.base_url = base_url.rstrip("/")
        self.retries = retries
        self.backoff = backoff
        self.client = httpx.Client(timeout=timeout, transport=transport,
                                   headers={"User-Agent": "hashhedge-quant/0.1"})

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _sleep_backoff(self, attempt: int, retry_after: float | None = None) -> None:
        delay = retry_after if retry_after is not None else min(60.0, self.backoff * 2 ** attempt)
        time.sleep(delay + random.uniform(0, 0.25 * self.backoff))

    def _after(self, resp: httpx.Response) -> None:  # хук для учёта веса
        pass

    def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        url = f"{self.base_url}{path}"
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                resp = self.client.get(url, params=params)
            except httpx.TransportError as e:
                last = e
                log.warning("GET %s: %s (попытка %d)", path, e, attempt + 1)
                self._sleep_backoff(attempt)
                continue
            self._after(resp)
            if resp.status_code in (418, 429):
                ra = resp.headers.get("Retry-After")
                wait = float(ra) if ra and ra.replace(".", "", 1).isdigit() else None
                log.warning("GET %s: HTTP %d, пауза %s c", path, resp.status_code, wait or "backoff")
                last = RestError(f"HTTP {resp.status_code}")
                self._sleep_backoff(attempt, wait)
                continue
            if resp.status_code >= 500:
                last = RestError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                log.warning("GET %s: HTTP %d (попытка %d)", path, resp.status_code, attempt + 1)
                self._sleep_backoff(attempt)
                continue
            if resp.status_code >= 400:
                raise RestError(f"GET {path} {params}: HTTP {resp.status_code}: {resp.text[:300]}")
            return resp.json()
        raise RestError(f"GET {path} {params}: ретраи исчерпаны: {last}")


def klines_weight(limit: int) -> int:
    if limit < 100:
        return 1
    if limit < 500:
        return 2
    if limit <= 1000:
        return 5
    return 10


class BinanceREST(_Http):
    """Binance USDⓈ-M futures (fapi) + spot exchangeInfo."""

    WEIGHT_LIMIT = 2400

    def __init__(self, base_url: str = BINANCE_FAPI, *, spot_url: str = BINANCE_SPOT,
                 weight_per_min: int = 1800, transport: httpx.BaseTransport | None = None, **kw):
        super().__init__(base_url, transport=transport, **kw)
        self.spot_url = spot_url.rstrip("/")
        self.weight = RateLimiter(weight_per_min, 60.0)
        self.funding_limiter = RateLimiter(450, 300.0)   # fundingRate + fundingInfo: 500/5 мин
        self._used_weight = 0

    def _after(self, resp: httpx.Response) -> None:
        used = resp.headers.get("X-MBX-USED-WEIGHT-1M") or resp.headers.get("x-mbx-used-weight-1m")
        if used and used.isdigit():
            self._used_weight = int(used)
            if self._used_weight > self.WEIGHT_LIMIT * 0.9:
                pause = 61 - time.gmtime().tm_sec
                log.warning("Binance weight %d/%d — пауза %d c", self._used_weight, self.WEIGHT_LIMIT, pause)
                time.sleep(pause)

    # --- мета
    def exchange_info(self) -> dict:
        self.weight.acquire(1)
        return self.get("/fapi/v1/exchangeInfo")

    def funding_info(self) -> list[dict]:
        self.funding_limiter.acquire(1)
        return self.get("/fapi/v1/fundingInfo")

    def spot_exchange_info(self) -> dict:
        self.weight.acquire(20)
        url = f"{self.spot_url}/api/v3/exchangeInfo"
        resp = self.client.get(url, timeout=60.0)
        resp.raise_for_status()
        return resp.json()

    # --- ряды
    def klines(self, symbol: str, start_ms: int, end_ms: int, *, kind: str = "klines",
               interval: str = "1m", limit: int = 1500) -> list[list]:
        """Бары [start_ms, end_ms) — только полностью закрытые к моменту запроса.

        kind: klines | markPriceKlines | indexPriceKlines (для index нужен pair).
        """
        path = {"klines": "/fapi/v1/klines", "markPriceKlines": "/fapi/v1/markPriceKlines",
                "indexPriceKlines": "/fapi/v1/indexPriceKlines"}[kind]
        key = "pair" if kind == "indexPriceKlines" else "symbol"
        out: list[list] = []
        cursor = start_ms
        now_ms = int(time.time() * 1000)
        while cursor < end_ms:
            self.weight.acquire(klines_weight(limit))
            rows = self.get(path, {key: symbol, "interval": interval, "startTime": cursor,
                                   "endTime": end_ms - 1, "limit": limit})
            if not rows:
                break
            for r in rows:
                if int(r[0]) >= end_ms:
                    continue
                if int(r[6]) >= now_ms:      # ещё не закрытый бар
                    continue
                out.append(r)
            nxt = int(rows[-1][0]) + MS_MIN
            if nxt <= cursor:
                break
            cursor = nxt
            if len(rows) < limit:
                break
        return out

    def funding_rate(self, symbol: str, start_ms: int, end_ms: int, *, limit: int = 1000) -> list[dict]:
        """История funding [start_ms, end_ms). Поле markPrice у старых записей пустое."""
        out: list[dict] = []
        cursor = start_ms
        while cursor < end_ms:
            self.funding_limiter.acquire(1)
            rows = self.get("/fapi/v1/fundingRate", {"symbol": symbol, "startTime": cursor,
                                                     "endTime": end_ms - 1, "limit": limit})
            if not rows:
                break
            out.extend(r for r in rows if int(r["fundingTime"]) < end_ms)
            nxt = int(rows[-1]["fundingTime"]) + 1
            if nxt <= cursor or len(rows) < limit:
                break
            cursor = nxt
        return out


class BybitREST(_Http):
    """Bybit V5 market (category=linear)."""

    def __init__(self, base_url: str = BYBIT_API, *, transport: httpx.BaseTransport | None = None, **kw):
        super().__init__(base_url, transport=transport, **kw)
        self.limiter = RateLimiter(500, 5.0)   # лимит 600/5 с на IP, берём с запасом

    def _v5(self, path: str, params: dict[str, Any]) -> dict:
        self.limiter.acquire(1)
        data = self.get(path, params)
        if data.get("retCode") != 0:
            raise RestError(f"Bybit {path} {params}: {data.get('retCode')} {data.get('retMsg')}")
        return data["result"]

    def instruments(self, *, category: str = "linear", symbol: str | None = None,
                    status: str | None = None) -> list[dict]:
        """instruments-info с пагинацией по cursor. Закрытые тикеры — запросом с symbol=..."""
        out: list[dict] = []
        cursor = ""
        while True:
            params: dict[str, Any] = {"category": category, "limit": 1000}
            if symbol:
                params["symbol"] = symbol
            if status:
                params["status"] = status
            if cursor:
                params["cursor"] = cursor
            res = self._v5("/v5/market/instruments-info", params)
            out.extend(res.get("list") or [])
            cursor = res.get("nextPageCursor") or ""
            if not cursor or symbol:
                break
        return out

    def kline(self, symbol: str, start_ms: int, end_ms: int, *, interval: str = "1",
              category: str = "linear", limit: int = 1000) -> list[list]:
        """Бары [start_ms, end_ms) по возрастанию. Bybit отдаёт от новых к старым, ≤1000 за запрос."""
        step = limit * MS_MIN
        out: dict[int, list] = {}
        now_ms = int(time.time() * 1000)
        cursor = start_ms
        while cursor < end_ms:
            win_end = min(end_ms, cursor + step)
            res = self._v5("/v5/market/kline", {"category": category, "symbol": symbol,
                                                "interval": interval, "start": cursor,
                                                "end": win_end - 1, "limit": limit})
            for r in res.get("list") or []:
                t = int(r[0])
                if start_ms <= t < end_ms and t + MS_MIN <= now_ms:
                    out[t] = r
            cursor = win_end
        return [out[k] for k in sorted(out)]
