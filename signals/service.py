"""Сервис сигналов (раздел 8.2).

На закрытии бара каждого ТФ пересчитывает индикаторы, score и вероятности для всех инструментов,
публикует снапшот скринера; создаёт сигнал, только если одновременно:
P ≥ min_prob, EV ≥ min_ev_r, n ≥ min_n, risk governor разрешает, нет активного сигнала по символу/стороне,
бар не halted, сейчас не окно сброса, для TradFi открыта сессия базового рынка.
Исход каждого сигнала отслеживается по рыночным данным (lifecycle).

Фильтр широты рынка (стратегии с breadth_min_frac > 0, например feature_rules): решение о сигнале
откладывается, пока закрытие того же бара ТФ сигнала не придёт от всех символов со свежими данными (или не
истечёт breadth_grace_sec). Затем считается доля символов, у которых выполнены правила широты
(breadth_hit_<side> в кадре), и сигналы стороны с долей ниже порога снимаются — как в бэктесте, после правила
«один вход на эпизод». Символ, чей бар пришёл позже, использует уже посчитанную долю своего часа.
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import polars as pl
from sqlalchemy import select

from quant import instruments as I
from quant import metrics as M
from quant.backtest.challenge import RiskParams
from quant.bus import Bus
from quant.calibration.model import ev_r, live_check, load_active, prob_for
from quant.config import get_settings, load_challenge_config, load_strategy_config
from quant.db import session_scope
from quant.db.models import Alert, LiveSignal, SignalEvent
from quant.live.accounts import account_json, list_accounts, roll_day, to_propsim
from quant.risk.governor import plan_trade
from quant.sessions import is_open
from quant.strategies import get_strategy, load_params, with_overrides
from quant.timeutil import minute_of_day, now_utc, tf_minutes, to_min
from signals.lifecycle import OPEN_STATES, step
from signals.state import HTF_OF, SCREEN_TFS, SymbolState, on_bar, on_mark, warmup

log = logging.getLogger(__name__)

LIVE_DEFAULTS = {"min_prob": 0.5, "min_ev_r": 0.10, "min_n": 100, "require_calibration": True,
                 "watch_margin": 10.0, "stale_data_min": 5, "calibration_tol": 0.10, "breadth_grace_sec": 90}
BREADTH_KEEP = 48                                   # сколько последних часов широты помнить (для опоздавших баров)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def signal_json(s: LiveSignal) -> dict[str, Any]:
    return {
        "id": s.id, "created_at": _iso(s.created_at), "bar_ts": _iso(s.bar_ts), "hh_symbol": s.hh_symbol, "tf": s.tf,
        "side": s.side, "strategy": s.strategy, "score": s.score, "prob": s.prob, "ev_r": s.ev_r,
        "entry_type": s.entry_type, "entry": s.entry, "sl": s.sl, "tp1": s.tp1, "tp2": s.tp2,
        "sl_trigger": s.sl_trigger, "status": s.status, "outcome_r": s.outcome_r, "taken": s.taken,
        "reason": s.reason or {"pros": [], "cons": []}, "plan": (s.plan or {}).get("primary"),
        "expires_at": _iso(s.expires_at), "closed_at": _iso(s.closed_at),
    }


class SignalService:
    def __init__(self, bus: Bus, *, strategy: str = "confluence_v1", symbols: list[str] | None = None):
        self.bus = bus
        self.strategy_name = strategy
        self.scfg = load_strategy_config(strategy)
        self.live = {**LIVE_DEFAULTS, **(self.scfg.get("live") or {})}
        self.signal_tf = self.scfg.get("tf", "1h")
        self.insts = I.select(symbols) if symbols else list(I.instruments())
        self.state: dict[str, SymbolState] = {}
        self.cfg = load_challenge_config()
        self.risk = RiskParams.from_config(self.scfg.get("risk"))
        self.models = load_active(strategy)
        self.calibration_stale = False
        self.screener: dict[str, dict[str, dict]] = {tf: {} for tf in SCREEN_TFS}
        self.frames: dict[tuple[str, str], dict] = {}          # (sym, tf) → последняя строка кадра оценок
        self._strats: dict[tuple[str, str], Any] = {}
        # фильтр широты рынка: порог из параметров стратегии; стратегия в live считается без него (см. docstring)
        self.breadth_min = float((self.scfg.get("params") or {}).get("breadth_min_frac") or 0.0)
        self._breadth_pending: dict[int, dict[str, dict]] = {}
        self._breadth_done: dict[int, dict] = {}
        self._breadth_timers: dict[int, asyncio.Task] = {}
        self.breadth_last: dict | None = None
        self.ready = False

    # ------------------------------------------------------------------ запуск

    async def start(self, *, warm: bool = True) -> None:
        if warm:
            t0 = time.time()
            for inst in self.insts:
                try:
                    self.state[inst.hh_symbol] = await asyncio.to_thread(warmup, inst)
                except Exception:
                    log.exception("прогрев %s не удался", inst.hh_symbol)
            log.info("прогрев %d символов: %.0f с", len(self.state), time.time() - t0)
            for sym in list(self.state):
                for tf in SCREEN_TFS:
                    await asyncio.to_thread(self._recompute, sym, tf)
            await self._publish_screener()
        self.ready = True
        await asyncio.gather(self._consume_bars(), self._consume_marks(), self._periodic())

    async def _consume_bars(self) -> None:
        async for bar in self.bus.subscribe("bars.1m", group="signals"):
            try:
                await self.handle_bar(bar)
            except Exception:
                log.exception("обработка бара %s", bar.get("hh_symbol"))

    async def _consume_marks(self) -> None:
        async for m in self.bus.subscribe("mark.1m", group="signals-mark"):
            st = self.state.get(m.get("hh_symbol"))
            if st is not None:
                on_mark(st, m)

    async def _periodic(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                with session_scope() as s:
                    roll_day(s)
                await self._check_data_age()
                if int(time.time()) // 60 % 10 == 0:
                    self.models = load_active(self.strategy_name)
                    await self._check_calibration()
            except Exception:
                log.exception("периодическая задача")

    # ------------------------------------------------------------------ бары

    async def handle_bar(self, bar: dict) -> None:
        sym = bar["hh_symbol"]
        st = self.state.get(sym)
        if st is None:
            inst = I.by_symbol().get(sym)
            if inst is None or inst.hh_symbol not in {i.hh_symbol for i in self.insts}:
                return
            st = self.state[sym] = await asyncio.to_thread(warmup, inst)
        M.CONSUMER_LAG.set(max(0.0, time.time() - (int(bar["ts"]) + 60_000) / 1000))
        closed = on_bar(st, bar)
        await self._advance_signals(sym, bar)
        changed = []
        for tf in closed:
            if tf in SCREEN_TFS:
                row = await asyncio.to_thread(self._recompute, sym, tf)
                if row:
                    changed.append((tf, row))
                if tf == self.signal_tf:
                    if self.breadth_min > 0:
                        await self._breadth_collect(sym, tf)
                    else:
                        await self._maybe_signal(sym, tf)
        # цена/возраст данных обновляются каждую минуту
        for tf in SCREEN_TFS:
            row = self.screener[tf].get(sym)
            if row:
                row["price"] = st.price
                row["change_24h_pct"] = _num(st.change_24h_pct())
                row["data_age_sec"] = 0
        for tf, row in changed:
            await self.bus.publish("screener", {"type": "update", "tf": tf, "updated_at": _iso(now_utc()), "rows": [row]})
        if changed:
            await self._publish_screener()

    def _strategy(self, sym: str, tf: str):  # noqa: ANN202
        key = (sym, tf)
        if key not in self._strats:
            inst = self.state[sym].inst
            params = with_overrides(load_params(self.strategy_name, inst.group), {"tf": tf, "htf": HTF_OF[tf]})
            if self.breadth_min > 0:        # фильтр широты применяет сервис после сбора часа (_breadth_finalize)
                params = with_overrides(params, {"breadth_min_frac": 0.0})
            self._strats[key] = (get_strategy(self.strategy_name, params), params)
        return self._strats[key]

    def _frame_last(self, sym: str, tf: str) -> tuple[dict | None, Any]:
        st = self.state[sym]
        bars = st.bars.get(tf)
        if bars is None or bars.height < 50:
            return None, None
        strat, params = self._strategy(sym, tf)
        meta = {"instrument": st.inst, "funding": st.funding, "metrics": st.metrics}
        btc = self.frames.get(("BTCUSDT", tf))
        if btc is not None and st.inst.group != "crypto_major":
            meta["btc_trend"] = float(btc.get("htf_trend") or 0)
        frame = strat.compute(bars, hh_symbol=sym, tf=tf, htf=st.bars.get(HTF_OF[tf]), bars_1m=st.m1.tail(1440),
                              meta=meta)
        if frame.is_empty():
            return None, strat
        return frame.tail(1).row(0, named=True), strat

    def _recompute(self, sym: str, tf: str) -> dict | None:
        if sym not in self.state:
            return None
        with M.RECOMPUTE.labels(tf).time():
            last, strat = self._frame_last(sym, tf)
        st = self.state[sym]
        inst = st.inst
        if last is None:
            return None
        self.frames[(sym, tf)] = last
        row = self._row(st, tf, last)
        self.screener[tf][sym] = row
        return row

    def _prob(self, inst, side: str, tf: str, score: float | None) -> dict | None:  # noqa: ANN001
        info = prob_for(self.models, inst.group, side, tf, score)
        if info is None:
            return {"p": None, "ci_low": None, "ci_high": None, "n": 0, "calibrated": False, "low_data": True} \
                if score is not None else None
        if self.calibration_stale:
            info = {**info, "calibrated": False}
        info["low_data"] = info["low_data"] or info["n"] < self.live["min_n"]
        return info

    def _ev(self, last: dict, side: str, prob: dict | None) -> float | None:
        if not prob or prob.get("p") is None:
            return None
        entry, sl, tp1 = _num(last.get(f"entry_{side}")), _num(last.get(f"sl_{side}")), _num(last.get(f"tp1_{side}"))
        if not entry or not sl or not tp1 or entry == sl:
            return None
        risk = abs(entry - sl)
        rr = abs(tp1 - entry) / risk
        fees = entry * 2 * self.cfg.market("crypto_tradfi").fees.taker / risk
        return ev_r(prob["p"], rr, fees)

    def _row(self, st: SymbolState, tf: str, last: dict) -> dict:
        inst = st.inst
        sl_score, ss_score = _num(last.get("score_long")), _num(last.get("score_short"))
        p_long, p_short = self._prob(inst, "long", tf, sl_score), self._prob(inst, "short", tf, ss_score)
        now = now_utc()
        session = is_open(inst.underlying_type, now)
        active, active_side = self._active_signal(inst.hh_symbol)
        thr = min(float((last.get("thr_long") or 65)), 65.0)
        if st.halted:
            status = "halted"
        elif not session:
            status = "off_session"
        elif active is not None:
            status = "signal"
        elif max(sl_score or 0, ss_score or 0) >= thr - self.live["watch_margin"]:
            status = "watch"
        else:
            status = "none"
        return {
            "hh_symbol": inst.hh_symbol, "base": inst.base, "asset_class": inst.asset_class, "group": inst.group,
            "price": st.price, "change_24h_pct": _num(st.change_24h_pct()),
            "htf_trend": int(_num(last.get("htf_trend")) or 0), "rsi14": _num(last.get("rsi14")),
            "adx": _num(last.get("adx")), "vwap_dist_pct": _num(last.get("vwap_dist_pct")),
            "atr_pct": _num(last.get("atr_pct")), "funding_rate": _num(last.get("funding_rate")),
            "oi_change_24h_pct": _num(last.get("oi_change_pct")), "cvd_trend": int(_num(last.get("cvd_trend")) or 0),
            "score_long": sl_score, "score_short": ss_score, "p_long": p_long, "p_short": p_short,
            "ev_long_r": self._ev(last, "long", p_long), "ev_short_r": self._ev(last, "short", p_short),
            "status": status, "active_signal_id": active, "active_signal_side": active_side,
            "max_per_lot_usd": inst.max_per_lot_usd,
            "max_total_usd": inst.max_total_usd, "session_open": session,
            "data_age_sec": int(time.time() - st.last_bar_ms / 1000) - 60 if st.last_bar_ms else None,
        }

    async def _publish_screener(self) -> None:
        for tf in SCREEN_TFS:
            rows = sorted(self.screener[tf].values(), key=lambda r: r["hh_symbol"])
            await self.bus.set_state(f"screener:{tf}", {"tf": tf, "updated_at": _iso(now_utc()), "rows": rows})

    # ------------------------------------------------------------------ сигналы

    def _active_signal(self, sym: str) -> tuple[int | None, str | None]:
        with session_scope() as s:
            row = s.execute(select(LiveSignal.id, LiveSignal.side).where(
                LiveSignal.hh_symbol == sym, LiveSignal.status.in_(OPEN_STATES)).limit(1)).first()
            return (row[0], row[1]) if row else (None, None)

    async def _maybe_signal(self, sym: str, tf: str) -> None:
        last = self.frames.get((sym, tf))
        st = self.state[sym]
        if last is None or st.halted:
            return
        now = now_utc()
        mk = self.cfg.market(st.inst.market)
        mod = minute_of_day(to_min(now))
        if mk.day_boundary_min <= mod < mk.day_boundary_min + mk.reset_window_minutes:
            return
        if not is_open(st.inst.underlying_type, now):
            return
        strat, params = self._strategy(sym, tf)
        sigs = strat.signals_from(pl.DataFrame([last]), hh_symbol=sym, tf=tf)
        for r in sigs.iter_rows(named=True):
            side = "long" if r["side"] == 1 else "short"
            prob = self._prob(st.inst, side, tf, r["score"])
            ev = self._ev(last, side, prob)
            if self.live["require_calibration"]:
                if not prob or not prob.get("calibrated") or prob.get("p") is None:
                    continue
                if prob["p"] < self.live["min_prob"] or (ev or -1) < self.live["min_ev_r"] or prob["n"] < self.live["min_n"]:
                    continue
            await self._create(st, tf, r, last, side, prob, ev, strat, params)

    # ------------------------------------------------------------------ широта рынка

    def _breadth_expected(self, key: int) -> int:
        """Сколько символов должно прислать закрытие бара key: не halted и со свежими 1m-данными к этому моменту."""
        lo = key - (int(self.live["stale_data_min"]) + 1) * 60_000
        return sum(1 for st in self.state.values() if st.last_bar_ms is not None and st.last_bar_ms >= lo and not st.halted)

    async def _breadth_collect(self, sym: str, tf: str) -> None:
        last = self.frames.get((sym, tf))
        if last is None or last.get("close_ts") is None:
            return
        key = int(last["close_ts"].timestamp() * 1000)
        if key in self._breadth_done:                  # бар опоздал — доля часа уже посчитана
            await self._breadth_gate_and_signal(sym, tf, last, self._breadth_done[key])
            return
        self._breadth_pending.setdefault(key, {})[sym] = last
        if len(self._breadth_pending[key]) >= self._breadth_expected(key):
            await self._breadth_finalize(key, tf)
        elif key not in self._breadth_timers:
            self._breadth_timers[key] = asyncio.create_task(self._breadth_timer(key, tf))

    async def _breadth_timer(self, key: int, tf: str) -> None:
        await asyncio.sleep(float(self.live["breadth_grace_sec"]))
        if key not in self._breadth_done:
            await self._breadth_finalize(key, tf)

    async def _breadth_finalize(self, key: int, tf: str) -> None:
        if key in self._breadth_done:
            return
        rows = self._breadth_pending.pop(key, {})
        timer = self._breadth_timers.pop(key, None)
        if timer is not None and timer is not asyncio.current_task():
            timer.cancel()
        ready = [r for r in rows.values() if (_num(r.get("atr")) or 0) > 0]
        n = len(ready)
        b = {"close_ts_ms": key, "n_sym": n,
             "frac_long": sum(bool(r.get("breadth_hit_long")) for r in ready) / n if n else None,
             "frac_short": sum(bool(r.get("breadth_hit_short")) for r in ready) / n if n else None}
        self._breadth_done[key] = b
        for old in sorted(self._breadth_done)[:-BREADTH_KEEP]:
            self._breadth_done.pop(old, None)
        self.breadth_last = b
        log.info("широта рынка %s: символов %d, лонг %s, шорт %s", _iso(datetime.fromtimestamp(key / 1000, timezone.utc)),
                 n, b["frac_long"], b["frac_short"])
        for sym, row in rows.items():
            await self._breadth_gate_and_signal(sym, tf, row, b)

    async def _breadth_gate_and_signal(self, sym: str, tf: str, row: dict, b: dict) -> None:
        cur = self.frames.get((sym, tf))
        if cur is None or cur.get("close_ts") != row.get("close_ts"):
            return                                     # по символу уже закрылся следующий бар — этот сигнал устарел
        gated = dict(row)
        for side in ("long", "short"):
            frac = b.get(f"frac_{side}")
            gated[f"breadth_{side}"] = frac
            if frac is None or frac < self.breadth_min:
                gated[f"signal_{side}"] = False
        self.frames[(sym, tf)] = gated
        await self._maybe_signal(sym, tf)

    async def _create(self, st: SymbolState, tf: str, r: dict, last: dict, side: str, prob: dict | None,
                      ev: float | None, strat, params: dict) -> None:  # noqa: ANN001
        sym = st.inst.hh_symbol
        with session_scope() as s:
            exists = s.scalar(select(LiveSignal.id).where(LiveSignal.hh_symbol == sym, LiveSignal.side == side,
                                                          LiveSignal.status.in_(OPEN_STATES)).limit(1))
            if exists:
                return
            marks = await self._marks()
            plans = {}
            primary = None
            sgn = 1 if side == "long" else -1
            for acc_row in list_accounts(s, active_only=True):
                acc = to_propsim(acc_row, marks)
                alt_risk = sum(p.qty * abs(p.entry_price - (p.sl or p.entry_price)) for p in acc_row.positions
                               if I.by_symbol().get(p.hh_symbol) and I.by_symbol()[p.hh_symbol].group == "crypto_alt")
                plan = plan_trade(acc, st.inst, side=sgn, entry=r["entry_price"], sl=r["sl"], risk=self.risk,
                                  alt_open_risk_usd=alt_risk).as_dict()
                plans[str(acc_row.id)] = {**plan, "account_id": acc_row.id, "account": acc_row.name}
                if primary is None and plan["blocked_reason"] is None:
                    primary = plans[str(acc_row.id)]
            if plans and primary is None:
                # governor не разрешает ни на одном счёте — сигнал не создаётся (8.2)
                return
            factors = strat.explain(last, side)
            risk = abs(r["entry_price"] - r["sl"])
            rr1 = abs(r["tp1"] - r["entry_price"]) / risk if risk else None
            rr2 = abs(r["tp2"] - r["entry_price"]) / risk if risk and r["tp2"] is not None else None
            plan_json = {**(primary or {}), "side": side, "entry_type": r["entry_type"], "entry": r["entry_price"],
                         "sl": r["sl"], "sl_type": "stop_market", "sl_trigger": params.get("sl_trigger", "last"),
                         "tp1": r["tp1"], "tp2": r["tp2"], "tp1_frac": r["tp1_frac"], "rr1": rr1, "rr2": rr2}
            now = now_utc()
            sig = LiveSignal(bar_ts=r["ts"], hh_symbol=sym, tf=tf, side=side, strategy=self.strategy_name,
                             score=float(r["score"]), prob=prob, ev_r=ev, entry_type=r["entry_type"],
                             entry=float(r["entry_price"]), sl=float(r["sl"]), tp1=float(r["tp1"]),
                             tp2=_num(r["tp2"]), tp1_frac=float(r["tp1_frac"]),
                             sl_trigger=params.get("sl_trigger", "last"),
                             plan={"primary": {**plan_json, "expires_at": _iso(now + timedelta(minutes=int(r["valid_min"])))},
                                   "accounts": plans},
                             reason={"pros": [f.label for f in factors if f.verdict == "pro"],
                                     "cons": [f.label for f in factors if f.verdict == "con"]},
                             components={k: _num(v) for k, v in r.items() if k.startswith("c_")},
                             status="new", expires_at=now + timedelta(minutes=int(r["valid_min"])),
                             hold_until=now + timedelta(minutes=int(r["max_hold_min"])), source=st.source)
            s.add(sig)
            s.flush()
            payload = signal_json(sig)
        M.SIGNALS.labels(side, tf).inc()
        log.info("сигнал %s %s %s score=%.0f", sym, tf, side, r["score"])
        await self.bus.publish("signals", {"type": "new", "signal": payload})

    async def _advance_signals(self, sym: str, bar: dict) -> None:
        events = []
        with session_scope() as s:
            sigs = s.scalars(select(LiveSignal).where(LiveSignal.hh_symbol == sym,
                                                      LiveSignal.status.in_(OPEN_STATES))).all()
            for sig in sigs:
                before = sig.status
                trs = step(sig, bar)
                for t in trs:
                    s.add(SignalEvent(signal=sig, from_status=before, to_status=t.to, detail=t.detail))
                    before = t.to
                    if t.to in ("tp", "sl", "timeout", "expired", "invalidated"):
                        sig.closed_at = now_utc()
                        if "outcome_r" in t.detail:
                            sig.outcome_r = t.detail["outcome_r"]
                if trs:
                    events.append(signal_json(sig))
        for e in events:
            await self.bus.publish("signals", {"type": "status", "signal": e})

    async def _marks(self) -> dict[str, float]:
        out = {}
        for sym, st in self.state.items():
            v = st.last_mark if st.last_mark is not None else st.price
            if v is not None:
                out[sym] = v
        return out

    # ------------------------------------------------------------------ контроль

    async def _alert(self, kind: str, key: str, message: str) -> None:
        with session_scope() as s:
            recent = s.scalar(select(Alert.id).where(Alert.kind == kind, Alert.key == key,
                                                     Alert.ts >= now_utc() - timedelta(hours=1)).limit(1))
            if recent:
                return
            s.add(Alert(kind=kind, key=key, message=message))
        await self.bus.publish("alerts", {"kind": kind, "key": key, "message": message})

    async def _check_data_age(self) -> None:
        lim = self.live["stale_data_min"] * 60
        now = time.time()
        for sym, st in self.state.items():
            if st.last_bar_ms and now - st.last_bar_ms / 1000 > lim + 60:
                if st.inst.is_tradfi and not is_open(st.inst.underlying_type, now_utc()):
                    continue
                await self._alert("no_data", sym, f"{sym}: данных нет {int((now - st.last_bar_ms / 1000) / 60)} мин")

    async def _check_calibration(self) -> None:
        with session_scope() as s:
            rows = s.scalars(select(LiveSignal).where(LiveSignal.status.in_(("tp", "sl", "timeout")))
                             .order_by(LiveSignal.id.desc()).limit(500)).all()
            pred = np.array([r.prob["p"] for r in rows if r.prob and r.prob.get("p") is not None])
            obs = np.array([1.0 if r.status == "tp" else 0.0 for r in rows if r.prob and r.prob.get("p") is not None])
        if len(pred) < 30:
            return
        chk = live_check(pred, obs, tol=self.live["calibration_tol"])
        if chk["stale"] and not self.calibration_stale:
            await self._alert("calibration_stale", self.strategy_name,
                              "калибровка устарела: предсказанная и фактическая частота расходятся")
        self.calibration_stale = chk["stale"]

    async def accounts_snapshot(self) -> list[dict]:
        marks = await self._marks()
        with session_scope() as s:
            return [account_json(a, marks, self.risk.soft_daily_stop) for a in list_accounts(s)]
