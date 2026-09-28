"""Журнал сделок: запись при закрытии, серии Дугласа по 20 сделок, правило 6% Элдера, API."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.live.test_api import client, login  # noqa: F401 — фикстура API


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


def test_month_key_uses_prop_calendar():
    from quant.live.journal import month_key

    b = 20 * 60                                            # граница проп-дня 20:00 UTC = 00:00 UTC+4
    assert month_key(utc(2026, 9, 30, 19, 59), b) == "2026-09"
    assert month_key(utc(2026, 9, 30, 20, 0), b) == "2026-10"
    assert month_key(utc(2026, 12, 31, 21, 0), b) == "2027-01"
    assert month_key(utc(2026, 10, 1, 0, 0), 0) == "2026-10"


def _trade(i: int, pnl: float, risk: float | None = 100.0, signal: int | None = None):
    from quant.db.models import JournalTrade

    return JournalTrade(id=i, account_id=1, hh_symbol="BTCUSDT", side="long", qty=1.0, entry_price=100.0,
                        exit_price=100.0, sl=99.0 if risk else None, opened_at=utc(2026, 1, 1),
                        closed_at=utc(2026, 1, 1) + timedelta(hours=i), signal_id=signal, fees_usd=1.0, pnl_usd=pnl,
                        risk_usd=risk, r_multiple=pnl / risk if risk else None, exit_reason="manual", note="")


def test_series_of_twenty():
    from quant.live.journal import format_stats, journal_stats

    pnls = [100, -100, -100, -100, 200] * 5                   # 25 сделок: 3 убытка подряд в каждом цикле
    trades = [_trade(i, p, signal=i if i % 2 else None) for i, p in enumerate(pnls)]
    trades.append(_trade(99, -150))                           # убыток 1.5R — стоп не соблюдён
    trades.append(_trade(100, -50, risk=None))                # без стопа — нет R
    st = journal_stats(trades, expected={1: 0.2, 3: 0.4})
    assert st["series_size"] == 20
    s1, s2 = st["series"]
    assert s1["complete"] and s1["n"] == 20 and not s2["complete"] and s2["n"] == 7
    assert s1["avg_r"] == pytest.approx(0.0) and s1["max_loss_streak"] == 3
    assert s1["expected_avg_r"] == pytest.approx(0.3) and s1["n_expected"] == 2
    assert s2["stop_breaches"] == 1 and s2["without_stop"] == 1 and s2["n_r"] == 6
    tot = st["total"]
    assert tot["n"] == 27 and tot["pnl_usd"] == pytest.approx(-200.0)
    assert tot["profit_factor"] == pytest.approx(1500 / 1700)
    text = format_stats(st)
    assert "Серия 2: 7/20" in text and "Всего 27" in text
    assert "пуст" in format_stats(journal_stats([]))


def test_close_records_trade_and_month_rule(tmp_settings):
    from quant.backtest.challenge import RiskParams
    from quant.config import load_challenge_config
    from quant.db import session_scope
    from quant.db.models import AccountMonth, JournalTrade
    from quant.instruments import get
    from quant.live.accounts import add_position, close_position, create_account, to_propsim
    from quant.live.journal import month_status
    from quant.risk.governor import plan_trade

    taker = load_challenge_config().market("crypto_tradfi").fees.taker
    with session_scope() as s:
        row = create_account(s, name="A", challenge_type="two_stage", size=10_000)
        assert s.query(AccountMonth).count() == 1                        # месяц заведён при создании счёта
        add_position(s, row, hh_symbol="ETHUSDT", side="short", qty=2.0, entry_price=2_000.0, sl=2_050.0)
        t = close_position(s, row, row.positions[0].id, 2_100.0, reason="sl", note="стоп проскочил")
        fees = 2.0 * (2_000.0 + 2_100.0) * taker
        assert t.pnl_usd == pytest.approx(-200.0 - fees)
        assert t.risk_usd == pytest.approx(100.0) and t.r_multiple == pytest.approx((-200.0 - fees) / 100.0)
        assert t.exit_reason == "sl" and t.note == "стоп проскочил"
        assert row.balance == pytest.approx(10_000 - 200.0 - fees)             # баланс: та же сумма
        assert s.query(JournalTrade).count() == 1

        # правило 6%: убыток месяца ~203$ + риск открытой позиции 400$ ≥ 600$ → вход запрещён
        add_position(s, row, hh_symbol="BTCUSDT", side="long", qty=0.1, entry_price=100_000.0, sl=96_000.0)
        m = month_status(s, row, {}, 0.06)
        assert m["enabled"] and m["limit_usd"] == pytest.approx(600.0)
        assert m["used_usd"] == pytest.approx(10_000 - row.balance + 400.0)
        assert m["blocked"]
        acc = to_propsim(row, {})
        risk = RiskParams(max_correlated_exposure_pct=0.0)
        plan = plan_trade(acc, get("SOLUSDT"), side=1, entry=100.0, sl=95.0, risk=risk, month_room_usd=m["room_usd"])
        assert plan.blocked_reason and "6%" in plan.blocked_reason
        free = plan_trade(acc, get("SOLUSDT"), side=1, entry=100.0, sl=95.0, risk=risk)
        ok = plan_trade(acc, get("SOLUSDT"), side=1, entry=100.0, sl=95.0, risk=risk, month_room_usd=10.0)
        assert free.risk_usd > 10.0                                         # без правила риск больше
        assert ok.blocked_reason is None and ok.risk_usd == pytest.approx(10.0, rel=1e-6)
        off = month_status(s, row, {}, 0.0)
        assert not off["enabled"] and off["room_usd"] is None and not off["blocked"]


def test_journal_api(client):  # noqa: F811
    login(client)
    a = client.post("/api/accounts", json={"name": "J", "challenge_type": "two_stage", "size": 10_000}).json()
    assert a["month"]["enabled"] and a["month"]["limit_pct"] == pytest.approx(0.06)
    assert a["month"]["start_balance"] == pytest.approx(10_000)
    a = client.post(f"/api/accounts/{a['id']}/positions",
                    json={"hh_symbol": "BTCUSDT", "side": "long", "qty": 0.1, "entry_price": 100_000,
                          "sl": 99_000}).json()
    pid = a["positions"][0]["id"]
    assert client.delete(f"/api/accounts/{a['id']}/positions/{pid}?exit_price=101000&reason=bad").status_code == 422
    r = client.delete(f"/api/accounts/{a['id']}/positions/{pid}",
                      params={"exit_price": 101_000, "reason": "tp", "note": "по плану"})
    assert r.status_code == 200 and r.json()["positions"] == []
    j = client.get("/api/journal", params={"account_id": a["id"]}).json()
    assert j["total"] == 1
    t = j["items"][0]
    assert t["exit_reason"] == "tp" and t["note"] == "по плану" and t["risk_usd"] == pytest.approx(100.0)
    assert t["r_multiple"] == pytest.approx(t["pnl_usd"] / 100.0) and t["pnl_usd"] < 100.0      # минус комиссии
    t2 = client.patch(f"/api/journal/{t['id']}", json={"note": "вход рано", "exit_reason": "manual"}).json()
    assert t2["note"] == "вход рано" and t2["exit_reason"] == "manual"
    assert client.patch(f"/api/journal/{t['id']}", json={"exit_reason": "x"}).status_code == 422
    assert client.patch("/api/journal/999", json={"note": "?"}).status_code == 404
    st = client.get("/api/journal/stats", params={"account_id": a["id"]}).json()
    assert st["series_size"] == 20 and st["total"]["n"] == 1 and st["series"][0]["n"] == 1
    assert client.get("/api/journal", params={"account_id": a["id"], "from": "2099-01-01"}).json()["total"] == 0
    assert client.delete(f"/api/accounts/{a['id']}").json() == {"ok": True}                # журнал счёта — вместе с ним
    assert client.get("/api/journal").json()["total"] == 0
