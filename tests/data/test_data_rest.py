"""REST-клиенты: пагинация по времени и cursor, ретраи 429/5xx, только закрытые бары; CLI."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from quant.data.rest import BinanceREST, BybitREST, RateLimiter, RestError, klines_weight

MIN = 60_000
T0 = 1_704_067_200_000          # 2024-01-01


def _q(request: httpx.Request) -> dict:
    return {k: v[0] for k, v in parse_qs(urlparse(str(request.url)).query).items()}


def test_binance_klines_paginates_by_time_and_retries_429():
    calls = []
    state = {"429": 1}

    def handler(request):
        q = _q(request)
        calls.append(q)
        if state["429"]:
            state["429"] -= 1
            return httpx.Response(429, headers={"Retry-After": "0"})
        start, end, limit = int(q["startTime"]), int(q["endTime"]), int(q["limit"])
        rows = [[t, "1", "1", "1", "1", "0", t + MIN - 1, "0", 0, "0", "0", "0"]
                for t in range(start, min(end + 1, start + limit * MIN), MIN)]
        return httpx.Response(200, json=rows, headers={"X-MBX-USED-WEIGHT-1M": "10"})

    with BinanceREST("https://fapi.test", transport=httpx.MockTransport(handler), backoff=0.0) as b:
        rows = b.klines("BTCUSDT", T0, T0 + 3000 * MIN, limit=1500)
    assert len(rows) == 3000 and rows[0][0] == T0 and rows[-1][0] == T0 + 2999 * MIN
    assert [int(c["startTime"]) for c in calls[1:]] == [T0, T0 + 1500 * MIN]   # первый — 429
    assert all(int(c["endTime"]) == T0 + 3000 * MIN - 1 for c in calls[1:])


def test_binance_klines_drops_unclosed_bar():
    now = int(time.time() * 1000) // MIN * MIN

    def handler(request):
        return httpx.Response(200, json=[[now - MIN, "1", "1", "1", "1", "0", now - 1, "0", 0, "0", "0", "0"],
                                         [now, "1", "1", "1", "1", "0", now + MIN - 1, "0", 0, "0", "0", "0"]])

    with BinanceREST("https://fapi.test", transport=httpx.MockTransport(handler)) as b:
        rows = b.klines("BTCUSDT", now - MIN, now + MIN)
    assert [r[0] for r in rows] == [now - MIN]


def test_binance_5xx_then_error():
    n = {"c": 0}

    def handler(request):
        n["c"] += 1
        return httpx.Response(503, text="busy")

    with BinanceREST("https://fapi.test", transport=httpx.MockTransport(handler), retries=2, backoff=0.0) as b:
        with pytest.raises(RestError):
            b.exchange_info()
    assert n["c"] == 3
    with BinanceREST("https://fapi.test", transport=httpx.MockTransport(lambda r: httpx.Response(400, text="bad"))) as b:
        with pytest.raises(RestError):
            b.exchange_info()


def test_funding_rate_pagination():
    times = [T0 + i * 8 * 3600_000 + 1 for i in range(2500)]

    def handler(request):
        q = _q(request)
        s, e, lim = int(q["startTime"]), int(q["endTime"]), int(q["limit"])
        sel = [t for t in times if s <= t <= e][:lim]
        return httpx.Response(200, json=[{"symbol": "BTCUSDT", "fundingTime": t, "fundingRate": "0.0001",
                                          "markPrice": ""} for t in sel])

    with BinanceREST("https://fapi.test", transport=httpx.MockTransport(handler)) as b:
        rows = b.funding_rate("BTCUSDT", T0, times[-1] + 1)
    assert [r["fundingTime"] for r in rows] == times


def test_bybit_instruments_cursor_and_kline_order():
    pages = {"": (["A", "B"], "c1"), "c1": (["C"], "")}

    def handler(request):
        q = _q(request)
        if request.url.path.endswith("instruments-info"):
            if "symbol" in q:
                return httpx.Response(200, json={"retCode": 0, "result": {"list": [{"symbol": q["symbol"],
                                                                                   "status": "Closed"}]}})
            syms, nxt = pages[q.get("cursor", "")]
            return httpx.Response(200, json={"retCode": 0, "result": {
                "list": [{"symbol": s, "status": "Trading"} for s in syms], "nextPageCursor": nxt}})
        s, e = int(q["start"]), int(q["end"])
        rows = [[str(t), "1", "1", "1", "1", "0", "0"] for t in range(s, e + 1, MIN)][::-1]   # от новых к старым
        return httpx.Response(200, json={"retCode": 0, "result": {"list": rows[:1000]}})

    with BybitREST("https://bybit.test", transport=httpx.MockTransport(handler)) as y:
        assert [x["symbol"] for x in y.instruments()] == ["A", "B", "C"]
        assert y.instruments(symbol="FETUSDT")[0]["status"] == "Closed"
        k = y.kline("BTCUSDT", T0, T0 + 2500 * MIN)
    ts = [int(r[0]) for r in k]
    assert ts == list(range(T0, T0 + 2500 * MIN, MIN))


def test_bybit_error_code():
    h = httpx.MockTransport(lambda r: httpx.Response(200, json={"retCode": 10001, "retMsg": "params error"}))
    with BybitREST("https://bybit.test", transport=h) as y:
        with pytest.raises(RestError):
            y.instruments()


def test_rate_limiter_and_weights():
    rl = RateLimiter(3, 0.2)
    t = time.monotonic()
    for _ in range(4):
        rl.acquire()
    assert time.monotonic() - t >= 0.15
    assert [klines_weight(x) for x in (50, 499, 1000, 1500)] == [1, 2, 5, 10]


def test_cli_registers_data_and_instruments():
    from quant.cli import build_parser

    p = build_parser()
    a = p.parse_args(["data", "backfill", "--symbols", "BTCUSDT,ETHUSDT", "--datasets", "klines,mark",
                      "--start", "2022-01", "--workers", "6"])
    assert a.func.__name__ == "cmd_backfill" and a.symbols == ["BTCUSDT,ETHUSDT"]
    assert p.parse_args(["instruments", "validate"]).func.__name__ == "cmd_validate"
    for cmd in ("build", "quality", "fill-gaps", "status"):
        assert p.parse_args(["data", cmd]).func.__name__.startswith("cmd_")
    assert p.parse_args(["data", "rebuild-clickhouse", "--url", "http://x:8123"]).url == "http://x:8123"
    with pytest.raises(SystemExit) as e:
        p.parse_args(["data", "--help"])
    assert e.value.code == 0


def test_resolve_datasets_and_parse_end():
    from quant.data.backfill import parse_end
    from quant.data.datasets import resolve
    from quant.timeutil import utc

    assert resolve("klines,mark,funding,metrics,bookDepth,aggTrades") == \
        ["klines_1m", "markPriceKlines_1m", "fundingRate", "metrics", "bookDepth", "aggTrades"]
    with pytest.raises(ValueError):
        resolve("trades")
    assert parse_end("2026-08") == utc(2026, 9, 1)
    assert parse_end("2026-12") == utc(2027, 1, 1)
    assert parse_end("2026-09-21") == utc(2026, 9, 22)
