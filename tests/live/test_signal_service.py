"""Сервис сигналов целиком на синтетике: бары → скринер → сигнал с планом → жизненный цикл → шина."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import func, select

from quant.backtest.synthetic import random_walk_bars
from quant.data.aggregate import resample
from quant.instruments import get
from quant.timeutil import utc


@pytest.fixture
def service(tmp_settings, monkeypatch):
    import signals.service as svc
    from quant.bus import MemoryBus
    from signals.state import SymbolState, all_tfs

    start = utc(2026, 3, 2)
    bars = random_walk_bars(6 * 1440, seed=21, start=start, price=60_000, sigma=0.0008)
    warm, rest = bars.head(4 * 1440), bars.slice(4 * 1440)

    def fake_warmup(inst, **_):
        st = SymbolState(inst=inst, m1=warm.tail(2 * 1440))
        for tf in all_tfs():
            st.bars[tf] = resample(warm, tf)
        st.last_bar_ms = int(warm["ts"][-1].timestamp() * 1000)
        return st

    monkeypatch.setattr(svc, "warmup", fake_warmup)
    bus = MemoryBus()
    s = svc.SignalService(bus, strategy="baseline_random", symbols=["BTCUSDT"])
    s.live["require_calibration"] = False
    s.state["BTCUSDT"] = fake_warmup(get("BTCUSDT"))
    return s, bus, rest


def test_bars_to_screener_and_signals(service):
    from quant.db import session_scope
    from quant.db.models import LiveSignal, SignalEvent
    from quant.live.accounts import create_account

    svc, bus, rest = service
    with session_scope() as s:
        create_account(s, name="Boost", challenge_type="two_stage", size=10_000)
    events = []

    async def run():
        async def listen():
            async for m in bus.subscribe("signals"):
                events.append(m)
        task = asyncio.create_task(listen())
        await asyncio.sleep(0)
        for tf in ("15m", "1h", "4h"):
            svc._recompute("BTCUSDT", tf)
        for row in rest.iter_rows(named=True):
            await svc.handle_bar({**row, "ts": int(row["ts"].timestamp() * 1000), "hh_symbol": "BTCUSDT"})
        await asyncio.sleep(0.05)
        task.cancel()

    asyncio.run(run())
    row = svc.screener["1h"]["BTCUSDT"]
    assert row["price"] == pytest.approx(float(rest["close"][-1]))
    assert row["score_long"] is not None and row["p_long"]["calibrated"] is False
    with session_scope() as s:
        n = s.scalar(select(func.count()).select_from(LiveSignal))
        closed = s.scalar(select(func.count()).select_from(LiveSignal).where(
            LiveSignal.status.in_(("tp", "sl", "timeout", "expired", "invalidated"))))
        sig = s.scalars(select(LiveSignal).order_by(LiveSignal.id)).first()
        n_events = s.scalar(select(func.count()).select_from(SignalEvent))
        plan = sig.plan["primary"] if sig else None
    assert n > 0, "baseline_random должен дать сигналы за 2 дня"
    assert closed > 0 and n_events > 0
    assert plan["notional_usd"] > 0 and plan["blocked_reason"] is None and plan["sl_type"] == "stop_market"
    assert any(e["type"] == "new" for e in events) and any(e["type"] == "status" for e in events)


def test_no_signals_in_reset_window(service, monkeypatch):
    import signals.service as svc_mod

    svc, bus, rest = service
    monkeypatch.setattr(svc_mod, "now_utc", lambda: utc(2026, 3, 6, 20, 5))
    svc._recompute("BTCUSDT", "1h")
    asyncio.run(svc._maybe_signal("BTCUSDT", "1h"))
    from quant.db import session_scope
    from quant.db.models import LiveSignal

    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(LiveSignal)) == 0
