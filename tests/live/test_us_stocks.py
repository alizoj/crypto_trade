"""Фаза 8: загрузчик Alpaca (на подменённом HTTP) — пагинация, регулярная сессия, запись/чтение."""

from __future__ import annotations

import httpx

from quant.data.us_stocks import AlpacaClient, backfill, load_us_bars
from quant.timeutil import utc


def _handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["APCA-API-KEY-ID"] == "k"
    token = request.url.params.get("page_token")
    assert request.url.params["adjustment"] == "all"
    if token is None:
        bars = [{"t": "2026-03-03T13:29:00Z", "o": 1, "h": 1, "l": 1, "c": 1, "v": 5, "n": 1, "vw": 1},   # премаркет
                {"t": "2026-03-03T14:30:00Z", "o": 200, "h": 201, "l": 199, "c": 200.5, "v": 1000, "n": 10, "vw": 200.2}]
        return httpx.Response(200, json={"bars": bars, "next_page_token": "p2"})
    bars = [{"t": "2026-03-03T20:59:00Z", "o": 202, "h": 203, "l": 201, "c": 202.5, "v": 900, "n": 9, "vw": 202.1},
            {"t": "2026-03-03T21:30:00Z", "o": 202, "h": 202, "l": 202, "c": 202, "v": 10, "n": 1, "vw": 202}]   # после закрытия
    return httpx.Response(200, json={"bars": bars, "next_page_token": None})


def test_alpaca_backfill_regular_session_only(tmp_settings):
    client = AlpacaClient("k", "s", transport=httpx.MockTransport(_handler))
    res = backfill(["AAPL"], utc(2026, 3, 3), utc(2026, 3, 4), client=client)
    assert res == {"AAPL": 2}
    df = load_us_bars("AAPL")
    # март 2026 до перехода на летнее время: сессия 14:30–21:00 UTC
    assert df["close"].to_list() == [200.5, 202.5]
    assert df["source"].to_list() == ["alpaca", "alpaca"]
