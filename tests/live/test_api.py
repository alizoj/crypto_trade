"""API по контракту docs/api_contract.md: авторизация (пароль), счета, сигналы, скринер, бэктесты."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_settings):
    from api.app import create_app
    from api.auth import create_user
    from quant.bus import MemoryBus

    bus = MemoryBus()
    create_user("trader", "pw-123456")
    asyncio.run(bus.set_state("screener:1h", {"tf": "1h", "updated_at": "2026-09-23T12:00:00Z", "rows": [
        {"hh_symbol": "BTCUSDT", "score_long": 70.0, "status": "watch"}]}))
    app = create_app(bus=bus)
    return TestClient(app)


def login(c: TestClient) -> None:
    r = c.post("/api/auth/login", json={"username": "trader", "password": "pw-123456"})
    assert r.status_code == 200, r.text


def test_auth_required(client):
    assert client.get("/api/meta").status_code == 401
    bad = client.post("/api/auth/login", json={"username": "trader", "password": "wrong"})
    assert bad.status_code == 401
    login(client)
    assert client.get("/api/auth/me").json() == {"username": "trader"}
    m = client.get("/api/meta").json()
    assert m["reset_window"] == {"start_utc": "20:00", "end_utc": "20:10"}
    assert m["timeframes"] == ["15m", "1h", "4h"]
    client.post("/api/auth/logout")
    assert client.get("/api/meta").status_code == 401


def test_screener_from_bus_state(client):
    login(client)
    r = client.get("/api/screener?tf=1h").json()
    assert r["rows"][0]["hh_symbol"] == "BTCUSDT"
    assert client.get("/api/screener?tf=2h").status_code == 422


def test_accounts_and_positions(client):
    login(client)
    a = client.post("/api/accounts", json={"name": "Boost", "challenge_type": "two_stage", "size": 10_000}).json()
    assert a["daily_floor"] == pytest.approx(9_500)
    assert a["total_floor"] == pytest.approx(9_000)
    assert a["target_usd"] == pytest.approx(10_800)
    a = client.post(f"/api/accounts/{a['id']}/positions",
                    json={"hh_symbol": "BTCUSDT", "side": "long", "qty": 0.1, "entry_price": 100_000}).json()
    assert len(a["positions"]) == 1 and a["trading_days"] == 1
    assert a["balance"] == pytest.approx(10_000 - 10_000 * 0.0003)
    bad = client.post(f"/api/accounts/{a['id']}/positions",
                      json={"hh_symbol": "BTCUSDT", "side": "short", "qty": 0.1, "entry_price": 100_000})
    assert bad.status_code == 400
    pid = a["positions"][0]["id"]
    a = client.delete(f"/api/accounts/{a['id']}/positions/{pid}?exit_price=101000").json()
    assert a["positions"] == []
    assert a["balance"] == pytest.approx(10_000 - 3 + 100 - 101_000 * 0.1 * 0.0003)
    a = client.patch(f"/api/accounts/{a['id']}", json={"balance": 10_500}).json()
    assert a["balance"] == 10_500
    assert len(client.get("/api/accounts").json()) == 1


def test_signals_journal_and_decision(client):
    from quant.db import session_scope
    from quant.db.models import LiveSignal
    from quant.timeutil import now_utc

    with session_scope() as s:
        s.add(LiveSignal(bar_ts=now_utc(), hh_symbol="SOLUSDT", tf="1h", side="long", strategy="confluence_v1",
                         score=78, prob={"p": 0.61, "ci_low": 0.55, "ci_high": 0.67, "n": 412, "calibrated": True,
                                         "low_data": False}, ev_r=0.38, entry_type="limit", entry=142.35, sl=139.8,
                         tp1=146.1, tp2=149.4, plan={"primary": {"rr1": 1.5}}, reason={"pros": ["x"], "cons": []},
                         status="active", expires_at=now_utc() + timedelta(hours=1)))
    login(client)
    r = client.get("/api/signals?symbol=solusdt").json()
    assert r["total"] == 1
    sid = r["items"][0]["id"]
    assert r["items"][0]["prob"]["n"] == 412
    d = client.post(f"/api/signals/{sid}/decision", json={"taken": True}).json()
    assert d["taken"] is True
    assert client.get("/api/calibration/live").json()["n_total"] == 0


def test_backtests_and_trials(client):
    from quant.optimize import registry

    rid = registry.record_run(kind="backtest", strategy="confluence_v1", params={}, dataset_hash="h", config={},
                              metrics={"p_pass_s1": 0.4, "full": {"x": 1}})
    registry.record_trial(study="s", level="research", strategy="confluence_v1", params={"a": 1},
                          dataset_hash="h", metrics={"avg_r": 0.1})
    login(client)
    runs = client.get("/api/backtests").json()
    assert runs[0]["id"] == rid and runs[0]["metrics"]["p_pass_s1"] == 0.4 and "full" not in runs[0]["metrics"]
    assert client.get("/api/trials").json()["total"] == 1


def test_bot_message_format():
    from bot.telegram import format_signal, in_quiet_hours
    from quant.timeutil import utc

    sig = {"id": 1, "hh_symbol": "SOLUSDT", "tf": "1h", "side": "long", "score": 78.0,
           "prob": {"p": 0.61, "ci_low": 0.55, "ci_high": 0.67, "n": 412, "calibrated": True},
           "ev_r": 0.38, "entry": 142.35, "entry_type": "limit", "sl": 139.8, "sl_trigger": "last", "tp1": 146.1,
           "tp2": 149.4, "plan": {"rr1": 1.5, "rr2": 2.8, "notional_usd": 12500, "risk_usd": 125,
                                  "risk_pct_daily_room": 0.1},
           "reason": {"pros": ["HTF аптренд"], "cons": ["ADX 19 — тренд слабый"]},
           "expires_at": "2026-09-23T15:00:00Z", "status": "active"}
    text = format_signal(sig, now=utc(2026, 9, 23, 14, 48))
    assert text.splitlines()[0] == "🟢 LONG SOLUSDT · 1H · score 78"
    assert "Вероятность: 61% (ДИ 55–67%, n=412) · EV +0.38R" in text
    assert "SL 139.8" in text and "(stop-market, last)" in text and "R:R 1.5/2.8" in text
    assert "(10% дневного запаса)" in text
    assert "Действует до 15:00 UTC · до ресета лимита 5ч 12м" in text
    assert in_quiet_hours("22:00-06:00", utc(2026, 1, 1, 23, 0))
    assert not in_quiet_hours("22:00-06:00", utc(2026, 1, 1, 12, 0))
