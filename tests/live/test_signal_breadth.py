"""Сервис сигналов с фильтром широты рынка: решение откладывается до сбора часа по всем символам."""

from __future__ import annotations

import asyncio
from datetime import timezone

import pytest
from sqlalchemy import select

from quant.backtest.synthetic import random_walk_bars
from quant.data.aggregate import resample
from quant.instruments import get
from quant.timeutil import utc

SYMS = ("BTCUSDT", "ETHUSDT")
PARAMS = {"rules_long": [["rsi14", "<=", 50]], "rules_short": [["rsi14", ">=", 50]], "rearm_bars": 3}


def _run(tmp_settings, monkeypatch, breadth_min: float):
    import signals.service as svc
    from quant.bus import MemoryBus
    from quant.db import session_scope
    from quant.db.models import LiveSignal
    from signals.state import SymbolState, all_tfs

    start = utc(2026, 3, 2)
    data = {s: random_walk_bars(6 * 1440, seed=31 + k, start=start, price=100.0 * (k + 1), sigma=0.0015)
            for k, s in enumerate(SYMS)}

    def fake_warmup(inst, **_):
        warm = data[inst.hh_symbol].head(4 * 1440)
        st = SymbolState(inst=inst, m1=warm.tail(2 * 1440))
        for tf in all_tfs():
            st.bars[tf] = resample(warm, tf)
        st.last_bar_ms = int(warm["ts"][-1].timestamp() * 1000)
        return st

    real_load = svc.load_params
    monkeypatch.setattr(svc, "warmup", fake_warmup)
    monkeypatch.setattr(svc, "load_params", lambda name, group=None: {**real_load(name, group), **PARAMS})
    s = svc.SignalService(MemoryBus(), strategy="feature_rules", symbols=list(SYMS))
    s.live["require_calibration"] = False
    s.live["breadth_grace_sec"] = 30             # полные часы закрываются сразу по приходу всех символов
    s.breadth_min = breadth_min
    for sym in SYMS:
        s.state[sym] = fake_warmup(get(sym))

    async def run():
        for tf in ("15m", "1h", "4h"):
            for sym in SYMS:
                s._recompute(sym, tf)
        rest = {sym: data[sym].slice(4 * 1440) for sym in SYMS}
        for i in range(rest[SYMS[0]].height):
            for sym in SYMS:
                row = rest[sym].row(i, named=True)
                await s.handle_bar({**row, "ts": int(row["ts"].timestamp() * 1000), "hh_symbol": sym})
        await asyncio.sleep(0.05)

    asyncio.run(run())
    with session_scope() as db:
        sigs = [(x.hh_symbol, x.side, x.bar_ts) for x in db.scalars(select(LiveSignal)).all()]
    return s, sigs


def test_breadth_gate_in_live(tmp_settings, monkeypatch):
    s, sigs = _run(tmp_settings, monkeypatch, breadth_min=1.0)
    assert s._breadth_done, "широта должна считаться на каждом закрытии часа"
    fracs = [b["frac_long"] for b in s._breadth_done.values()]
    assert all(b["n_sym"] == 2 for b in s._breadth_done.values())
    assert any(f < 1.0 for f in fracs) and any(f == 1.0 for f in fracs)     # фильтр реально что-то отсекает
    assert sigs, "в часы, когда правила выполнены на обоих символах, сигналы должны быть"
    for sym, side, bar_ts in sigs:
        ts = bar_ts if bar_ts.tzinfo else bar_ts.replace(tzinfo=timezone.utc)      # SQLite хранит без зоны
        b = s._breadth_done[int(ts.timestamp() * 1000)]
        assert b[f"frac_{side}"] == pytest.approx(1.0)


def test_breadth_gate_reduces_signals(tmp_settings, monkeypatch):
    _, gated = _run(tmp_settings, monkeypatch, breadth_min=1.0)
    from quant.db import session_scope
    from quant.db.models import LiveSignal

    with session_scope() as db:                    # чистая база для прогона без фильтра
        for x in db.scalars(select(LiveSignal)).all():
            db.delete(x)
    _, free = _run(tmp_settings, monkeypatch, breadth_min=1e-9)
    assert len(gated) < len(free)
