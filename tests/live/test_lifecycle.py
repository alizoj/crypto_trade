from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from signals.lifecycle import step

T0 = datetime(2026, 3, 2, 10, 0, tzinfo=timezone.utc)


@dataclass
class Sig:
    side: str = "long"
    entry_type: str = "market"
    entry: float = 100.0
    sl: float = 98.0
    tp1: float = 103.0
    sl_trigger: str = "last"
    status: str = "new"
    expires_at: datetime = T0 + timedelta(minutes=60)
    hold_until: datetime | None = T0 + timedelta(hours=10)
    fill_price: float | None = None
    triggered_at: datetime | None = None


def bar(minute: int, o: float, h: float, l: float, c: float) -> dict:
    return {"ts": int((T0 + timedelta(minutes=minute)).timestamp() * 1000), "open": o, "high": h, "low": l, "close": c}


def test_market_to_tp():
    s = Sig()
    tr = step(s, bar(1, 100, 100.5, 99.8, 100.2))
    assert [t.to for t in tr] == ["active", "triggered"] and s.fill_price == 100
    tr = step(s, bar(2, 100.2, 103.2, 100.1, 103))
    assert tr[-1].to == "tp" and tr[-1].detail["outcome_r"] == 1.5


def test_sl_first_on_ambiguous_bar():
    s = Sig(status="triggered", fill_price=100.0)
    tr = step(s, bar(3, 100, 103.5, 97.5, 101))
    assert tr[-1].to == "sl" and tr[-1].detail["ambiguous"]


def test_limit_expires_and_invalidates():
    s = Sig(entry_type="limit", entry=99.0)
    tr = step(s, bar(1, 100, 100.2, 99.0, 100))     # касание, не проход — нет входа
    assert s.status == "active"
    tr = step(s, bar(2, 100, 103.1, 99.5, 103))      # ушла к TP без входа
    assert tr[-1].to == "invalidated"
    s2 = Sig(entry_type="limit", entry=99.0)
    step(s2, bar(61, 100, 100.1, 99.9, 100))
    assert s2.status == "expired"


def test_timeout():
    s = Sig(status="triggered", fill_price=100.0, hold_until=T0 + timedelta(minutes=5))
    tr = step(s, bar(6, 100.5, 100.8, 100.4, 100.6))
    assert tr[-1].to == "timeout" and abs(tr[-1].detail["outcome_r"] - 0.3) < 1e-9
