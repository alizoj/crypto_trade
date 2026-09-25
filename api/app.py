"""FastAPI: REST + WebSocket по контракту docs/api_contract.md (фаза 7)."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
import polars as pl
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload

from api import auth
from api.market import MarketView
from quant import instruments as I
from quant.analysis.text import describe
from quant.backtest.challenge import RiskParams
from quant.bus import Bus, get_bus
from quant.calibration.model import live_check, load_active, prob_for, wilson
from quant.config import get_settings, load_challenge_config, load_strategy_config
from quant.db import session_scope
from quant.db.models import Account as AccountRow
from quant.db.models import BacktestRun, LiveSignal, Trial
from quant.live.accounts import account_json, add_position, close_position, create_account, list_accounts, to_propsim
from quant.risk.governor import plan_trade
from quant.sessions import is_open, next_change
from quant.timeutil import next_reset_min, now_utc, parse_dt, to_min, from_min
from signals.service import _iso, _num, signal_json

log = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parents[1]
WEB_DIST = ROOT / "web" / "dist"


class LoginIn(BaseModel):
    username: str
    password: str


class AccountIn(BaseModel):
    name: str
    challenge_type: str = "two_stage"
    size: float
    stage: str = "stage1"


class AccountPatch(BaseModel):
    name: str | None = None
    balance: float | None = None
    stage: str | None = None
    status: str | None = None


class PositionIn(BaseModel):
    hh_symbol: str
    side: str = Field(pattern="^(long|short)$")
    qty: float
    entry_price: float
    sl: float | None = None
    tp: float | None = None
    signal_id: int | None = None


class DecisionIn(BaseModel):
    taken: bool


def create_app(*, bus: Bus | None = None, service: Any | None = None, strategy: str = "confluence_v1") -> FastAPI:
    app = FastAPI(title="HashHedge Quant API", version="1.0")
    bus = bus or get_bus()
    mv = MarketView(bus, service, strategy)
    cfg = load_challenge_config()
    scfg = load_strategy_config(strategy)
    risk = RiskParams.from_config(scfg.get("risk"))
    settings = get_settings()
    User = Depends(auth.require_user)

    # ------------------------------------------------------------------ auth

    @app.post("/api/auth/login")
    def login(body: LoginIn, request: Request, response: Response):
        token = auth.login(body.username, body.password, request.client.host if request.client else "?")
        response.set_cookie(auth.COOKIE, token, httponly=True, samesite="lax", secure=settings.cookie_secure,
                            max_age=int(auth.SESSION_TTL.total_seconds()), path="/")
        return {"username": body.username}

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        auth.logout(request.cookies.get(auth.COOKIE))
        response.delete_cookie(auth.COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/auth/me")
    def me(user: str = User):
        return {"username": user}

    # ------------------------------------------------------------------ мета и скринер

    @app.get("/api/meta")
    def meta(user: str = User):
        mk = cfg.market("crypto_tradfi")
        b, w = mk.day_boundary_min, mk.reset_window_minutes
        return {
            "server_time": _iso(now_utc()),
            "reset_window": {"start_utc": f"{b // 60:02d}:{b % 60:02d}", "end_utc": f"{(b + w) // 60:02d}:{(b + w) % 60:02d}"},
            "next_reset": _iso(from_min(next_reset_min(to_min(now_utc()), b))),
            "calibrated": bool(load_active(strategy)),
            "calibration_stale": bool(getattr(service, "calibration_stale", False)),
            "timeframes": ["15m", "1h", "4h"],
            "asset_classes": ["crypto", "equity", "etf", "metal", "energy"],
            "groups": ["crypto_major", "crypto_alt", "tradfi"],
        }

    @app.get("/api/screener")
    async def screener(tf: str = Query("1h", pattern="^(15m|1h|4h)$"), user: str = User):
        return await mv.screener(tf)

    @app.websocket("/ws/screener")
    async def ws_screener(ws: WebSocket, tf: str = "1h"):
        if auth.ws_user(ws) is None:
            await ws.close(code=4401)
            return
        await ws.accept()
        state = {"tf": tf}
        snap = await mv.screener(tf)
        await ws.send_json({"type": "snapshot", **snap})

        async def pump(stream: str, kind: str):
            async for msg in bus.subscribe(stream):
                if kind == "screener":
                    if msg.get("tf") == state["tf"]:
                        await ws.send_json(msg)
                elif kind == "signals":
                    await ws.send_json({"type": "signal", "signal": msg.get("signal")})
                else:
                    await ws.send_json({"type": "account", "account": msg})

        async def pinger():
            while True:
                await asyncio.sleep(20)
                await ws.send_json({"type": "ping", "ts": _iso(now_utc())})

        async def reader():
            while True:
                msg = await ws.receive_json()
                if msg.get("type") == "tf" and msg.get("tf") in ("15m", "1h", "4h"):
                    state["tf"] = msg["tf"]
                    await ws.send_json({"type": "snapshot", **(await mv.screener(state["tf"]))})

        tasks = [asyncio.create_task(c) for c in (pump("screener", "screener"), pump("signals", "signals"),
                                                  pump("accounts", "accounts"), pinger(), reader())]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        except WebSocketDisconnect:
            pass
        finally:
            for t in tasks:
                t.cancel()

    # ------------------------------------------------------------------ инструменты

    @app.get("/api/instruments")
    def instruments(user: str = User):
        return [{"hh_symbol": i.hh_symbol, "base": i.base, "asset_class": i.asset_class, "group": i.group,
                 "market": i.market, "underlying_type": i.underlying_type, "max_per_lot_usd": i.max_per_lot_usd,
                 "max_total_usd": i.max_total_usd, "history_start": _iso(i.history_start), "binance_um": i.binance_um,
                 "bybit_linear": i.bybit_linear, "notes": i.notes} for i in I.instruments()]

    def _inst(sym: str) -> I.Instrument:
        inst = I.by_symbol().get(sym.upper())
        if inst is None:
            raise HTTPException(404, f"нет инструмента {sym}")
        return inst

    @app.get("/api/instruments/{sym}/bars")
    async def bars(sym: str, tf: str = "1h", limit: int = Query(500, le=3000), end: str | None = None,
                   user: str = User):
        inst = _inst(sym)
        payload = await asyncio.to_thread(mv.bars_payload, inst.hh_symbol, tf, limit, parse_dt(end) if end else None)
        with session_scope() as s:
            sigs = s.scalars(select(LiveSignal).where(LiveSignal.hh_symbol == inst.hh_symbol)
                             .order_by(LiveSignal.id.desc()).limit(100)).all()
            markers = [{"t": int(s_.bar_ts.timestamp()) if s_.bar_ts.tzinfo else int(s_.bar_ts.replace(tzinfo=now_utc().tzinfo).timestamp()),
                        "side": s_.side, "kind": "signal", "text": f"{s_.side} {s_.score:.0f} → {s_.status}", "signal_id": s_.id}
                       for s_ in sigs]
            active = next((signal_json(s_) for s_ in sigs if s_.status in ("new", "active", "triggered")), None)
        payload["markers"] = markers
        payload["active_signal"] = active
        return payload

    def _setup_stats(inst: I.Instrument, tf: str, side: str, score: float | None) -> dict | None:
        trades = _oos_trades(strategy)
        if trades is None or score is None:
            return None
        sgn = 1 if side == "long" else -1
        f = trades.filter((pl.col("status") == 0) & (pl.col("group") == inst.group) & (pl.col("side") == sgn)
                          & (pl.col("tf") == tf) & ((pl.col("score") - score).abs() <= 7.5))
        if f.is_empty():
            return {"n": 0, "winrate": None, "avg_r": None, "ci_low": None, "ci_high": None, "last10": []}
        k = int((f["label"] == 1).sum())
        lo, hi = wilson(k, f.height)
        last = trades.filter((pl.col("hh_symbol") == inst.hh_symbol) & (pl.col("side") == sgn) & (pl.col("status") == 0)) \
            .sort("entry_min").tail(10)["label"].to_list()
        return {"n": f.height, "winrate": k / f.height, "avg_r": float(f["r_net"].mean()), "ci_low": lo, "ci_high": hi,
                "last10": [{1: "tp", 0: "sl", 2: "timeout"}.get(x, "timeout") for x in last]}

    @app.get("/api/instruments/{sym}/analysis")
    async def analysis(sym: str, tf: str = "1h", account_id: int | None = None, user: str = User):
        inst = _inst(sym)
        last, strat, params = await asyncio.to_thread(mv.last_row, inst.hh_symbol, tf)
        models = load_active(strategy)
        marks = mv.marks()
        sides = {}
        for side in ("long", "short"):
            if last is None:
                sides[side] = {"score": None, "prob": None, "ev_r": None, "factors": [], "plan": None, "setup_stats": None}
                continue
            score = _num(last.get(f"score_{side}"))
            prob = prob_for(models, inst.group, side, tf, score)
            factors = strat.explain(last, side)
            entry, sl_, tp1, tp2 = (_num(last.get(f"{k}_{side}")) for k in ("entry", "sl", "tp1", "tp2"))
            plan = None
            ev = None
            if entry and sl_ and tp1:
                r = abs(entry - sl_)
                rr1 = abs(tp1 - entry) / r if r else None
                fees_r = entry * 2 * cfg.market(inst.market).fees.taker / r if r else 0.0
                if prob and rr1:
                    ev = prob["p"] * rr1 - (1 - prob["p"]) - fees_r
                plan = {"side": side, "entry_type": params.get("entry_type", "market"), "entry": entry, "sl": sl_,
                        "sl_type": "stop_market", "sl_trigger": params.get("sl_trigger", "last"), "tp1": tp1, "tp2": tp2,
                        "tp1_frac": params.get("tp1_frac", 1.0) if tp2 else 1.0, "rr1": rr1,
                        "rr2": abs(tp2 - entry) / r if (tp2 and r) else None, "account_id": None, "notional_usd": None,
                        "qty": None, "risk_usd": None, "risk_pct_daily_room": None, "risk_pct_total_room": None,
                        "blocked_reason": "нет счёта" if account_id is None else None,
                        "expires_at": _iso(now_utc() + timedelta(minutes=int(params.get("valid_min", 60))))}
                if account_id is not None:
                    with session_scope() as s:
                        row = s.get(AccountRow, account_id, options=[selectinload(AccountRow.positions)])
                        if row is not None:
                            acc = to_propsim(row, marks)
                            pl_ = plan_trade(acc, inst, side=1 if side == "long" else -1, entry=entry, sl=sl_, risk=risk)
                            plan.update(pl_.as_dict())
                            plan["account_id"] = account_id
            sides[side] = {
                "score": score, "prob": prob, "ev_r": ev,
                "factors": [{"name": f.name, "label": f.label, "value": f.value if not isinstance(f.value, float) or f.value == f.value else None,
                             "points": f.points, "max_points": f.max_points, "verdict": f.verdict} for f in factors],
                "plan": plan, "setup_stats": _setup_stats(inst, tf, side, score)}
        st = mv.symbol_state(inst.hh_symbol)
        btc = None
        if inst.group != "crypto_major":
            b = mv.last_row("BTCUSDT", tf)[0] if "BTCUSDT" in I.by_symbol() else None
            btc = int(_num(b.get("htf_trend")) or 0) if b else None
        ctx = {"hh_symbol": inst.hh_symbol, "tf": tf, "score_long": sides["long"]["score"],
               "score_short": sides["short"]["score"], "btc_trend": btc,
               "session_open": is_open(inst.underlying_type, now_utc()) if inst.is_tradfi else None,
               "funding_rate": _num(last.get("funding_rate")) if last else None, "is_tradfi": inst.is_tradfi}
        f_long = [f for f in (strat.explain(last, "long") if last else [])]
        f_short = [f for f in (strat.explain(last, "short") if last else [])]
        funding = st.funding if st.funding is not None else pl.DataFrame()
        oi = st.metrics if st.metrics is not None else pl.DataFrame()
        oi_col = next((c for c in ("sum_open_interest_value", "oi_value", "sum_open_interest") if c in oi.columns), None)
        text = describe(f_long, f_short, ctx)
        ts_iso = _iso(last["close_ts"]) if last and last.get("close_ts") else None
        llm = None
        if settings.llm_summary_enabled and last is not None:
            facts = {"symbol": inst.hh_symbol, "tf": tf, "bar_close": ts_iso, "text": text,
                     **{side: {"score": sides[side]["score"], "prob": sides[side]["prob"], "ev_r": sides[side]["ev_r"],
                               "factors": [{k: f[k] for k in ("label", "points", "verdict")} for f in sides[side]["factors"]],
                               "plan": {k: (sides[side]["plan"] or {}).get(k) for k in ("entry", "sl", "tp1", "tp2", "rr1", "blocked_reason")}}
                        for side in ("long", "short")}}
            llm = await asyncio.to_thread(_llm_cached, inst.hh_symbol, tf, ts_iso or "", _json_key(facts))
        return {
            "hh_symbol": inst.hh_symbol, "tf": tf, "ts": ts_iso,
            "long": sides["long"], "short": sides["short"],
            "text": text, "llm_summary": llm,
            "context": {
                "funding": [{"t": int(r["ts"].timestamp()), "v": float(r["rate"])} for r in funding.tail(200).iter_rows(named=True)]
                if funding.height else [],
                "oi": [{"t": int(r["ts"].timestamp()), "v": float(r[oi_col])} for r in oi.tail(300).iter_rows(named=True)
                       if r[oi_col] is not None] if oi_col else [],
                "btc_trend": btc,
                "session": {"open": is_open(inst.underlying_type, now_utc()),
                            "next_change": _iso(next_change(inst.underlying_type, now_utc()))} if inst.is_tradfi else None,
            },
        }

    # ------------------------------------------------------------------ сигналы

    @app.get("/api/signals")
    def signals(status: str | None = None, symbol: str | None = None, side: str | None = None,
                from_: str | None = Query(None, alias="from"), to: str | None = None,
                limit: int = Query(50, le=500), offset: int = 0, user: str = User):
        with session_scope() as s:
            q = select(LiveSignal)
            if status:
                q = q.where(LiveSignal.status.in_(status.split(",")))
            if symbol:
                q = q.where(LiveSignal.hh_symbol == symbol.upper())
            if side:
                q = q.where(LiveSignal.side == side)
            if from_:
                q = q.where(LiveSignal.created_at >= parse_dt(from_))
            if to:
                q = q.where(LiveSignal.created_at < parse_dt(to))
            total = s.scalar(select(func.count()).select_from(q.subquery()))
            items = s.scalars(q.order_by(LiveSignal.id.desc()).limit(limit).offset(offset)).all()
            return {"total": int(total or 0), "items": [signal_json(x) for x in items]}

    @app.post("/api/signals/{sid}/decision")
    async def decision(sid: int, body: DecisionIn, user: str = User):
        with session_scope() as s:
            sig = s.get(LiveSignal, sid)
            if sig is None:
                raise HTTPException(404, "нет сигнала")
            sig.taken = body.taken
            out = signal_json(sig)
        await bus.publish("signals", {"type": "decision", "signal": out})
        return out

    @app.get("/api/calibration/live")
    def calibration_live(group: str | None = None, side: str | None = None, user: str = User):
        with session_scope() as s:
            q = select(LiveSignal).where(LiveSignal.status.in_(("tp", "sl", "timeout")))
            if side:
                q = q.where(LiveSignal.side == side)
            rows = s.scalars(q.order_by(LiveSignal.id.desc()).limit(1000)).all()
            pairs = [(r.prob["p"], 1.0 if r.status == "tp" else 0.0) for r in rows
                     if r.prob and r.prob.get("p") is not None
                     and (group is None or I.by_symbol().get(r.hh_symbol) and I.by_symbol()[r.hh_symbol].group == group)]
        if not pairs:
            return {"buckets": [], "brier": None, "n_total": 0, "stale": False}
        p, o = np.array([a for a, _ in pairs]), np.array([b for _, b in pairs])
        chk = live_check(p, o)
        return {"buckets": [{"p_pred": b["p_pred"], "p_obs": b["p_obs"], "n": b["n"], "ci_low": b["ci_low"],
                             "ci_high": b["ci_high"]} for b in chk["buckets"]],
                "brier": chk["brier"], "n_total": chk["n_total"], "stale": chk["stale"]}

    # ------------------------------------------------------------------ счета

    def _acc_out(row: AccountRow) -> dict:
        return account_json(row, mv.marks(), risk.soft_daily_stop)

    async def _acc_changed(row_json: dict) -> None:
        await bus.publish("accounts", row_json)

    @app.get("/api/accounts")
    def accounts(user: str = User):
        with session_scope() as s:
            return [_acc_out(a) for a in list_accounts(s)]

    @app.post("/api/accounts")
    async def account_create(body: AccountIn, user: str = User):
        with session_scope() as s:
            try:
                row = create_account(s, name=body.name, challenge_type=body.challenge_type, size=body.size,
                                     stage=body.stage)
            except ValueError as e:
                raise HTTPException(400, str(e)) from e
            out = _acc_out(row)
        await _acc_changed(out)
        return out

    def _get_acc(s, aid: int) -> AccountRow:  # noqa: ANN001
        row = s.get(AccountRow, aid, options=[selectinload(AccountRow.positions)])
        if row is None:
            raise HTTPException(404, "нет счёта")
        return row

    @app.patch("/api/accounts/{aid}")
    async def account_patch(aid: int, body: AccountPatch, user: str = User):
        with session_scope() as s:
            row = _get_acc(s, aid)
            for k, v in body.model_dump(exclude_none=True).items():
                setattr(row, k, v)
            s.flush()
            out = _acc_out(row)
        await _acc_changed(out)
        return out

    @app.delete("/api/accounts/{aid}")
    def account_delete(aid: int, user: str = User):
        with session_scope() as s:
            s.delete(_get_acc(s, aid))
        return {"ok": True}

    @app.post("/api/accounts/{aid}/positions")
    async def position_add(aid: int, body: PositionIn, user: str = User):
        with session_scope() as s:
            row = _get_acc(s, aid)
            _inst(body.hh_symbol)
            opp = [p for p in row.positions if p.hh_symbol == body.hh_symbol.upper() and p.side != body.side]
            if opp:
                raise HTTPException(400, "встречная позиция по тому же инструменту запрещена")
            add_position(s, row, hh_symbol=body.hh_symbol.upper(), side=body.side, qty=body.qty,
                         entry_price=body.entry_price, sl=body.sl, tp=body.tp, signal_id=body.signal_id)
            if body.signal_id:
                sig = s.get(LiveSignal, body.signal_id)
                if sig is not None:
                    sig.taken = True
            s.flush()
            s.refresh(row)
            out = _acc_out(row)
        await _acc_changed(out)
        return out

    @app.delete("/api/accounts/{aid}/positions/{pid}")
    async def position_close(aid: int, pid: int, exit_price: float, user: str = User):
        with session_scope() as s:
            row = _get_acc(s, aid)
            try:
                close_position(s, row, pid, exit_price)
            except StopIteration:
                raise HTTPException(404, "нет позиции") from None
            s.refresh(row)
            out = _acc_out(row)
        await _acc_changed(out)
        return out

    # ------------------------------------------------------------------ бэктесты

    def _run_json(r: BacktestRun) -> dict:
        m = dict(r.metrics or {})
        m.pop("full", None)
        keys = ("p_pass_s1", "p_pass_s2", "p_pass_both", "p_fail_daily", "p_fail_total", "p_unresolved",
                "median_days_s1", "p90_days_s1", "ev_usd", "pf", "winrate", "avg_r", "n_trades")
        out = {k: m.get(k) for k in keys if m.get(k) is not None}
        if m.get("dsr_value") is not None:
            out["dsr"] = m["dsr_value"]
        if m.get("pbo_value") is not None:
            out["pbo"] = m["pbo_value"]
        return {"id": r.id, "created_at": _iso(r.created_at), "kind": r.kind, "strategy": r.strategy,
                "params_hash": r.params_hash, "dataset_hash": r.dataset_hash, "code_version": r.code_version,
                "metrics": out, "report_url": f"/api/backtests/{r.id}/report" if r.report_path else None}

    @app.get("/api/backtests")
    def backtests(kind: str | None = None, limit: int = Query(50, le=500), user: str = User):
        with session_scope() as s:
            q = select(BacktestRun)
            if kind:
                q = q.where(BacktestRun.kind == kind)
            return [_run_json(r) for r in s.scalars(q.order_by(BacktestRun.id.desc()).limit(limit)).all()]

    @app.get("/api/backtests/compare")
    def compare(ids: str, user: str = User):
        want = [int(x) for x in ids.split(",") if x.strip()]
        with session_scope() as s:
            return {"runs": [_run_json(r) for r in s.scalars(select(BacktestRun).where(BacktestRun.id.in_(want))).all()]}

    @app.get("/api/backtests/{rid}/report")
    def report(rid: int, user: str = User):
        with session_scope() as s:
            r = s.get(BacktestRun, rid)
            if r is None or not r.report_path or not Path(r.report_path).exists():
                raise HTTPException(404, "нет отчёта")
            return HTMLResponse(Path(r.report_path).read_text(encoding="utf-8"))

    @app.get("/api/trials")
    def trials(study: str | None = None, limit: int = Query(100, le=1000), user: str = User):
        with session_scope() as s:
            q = select(Trial)
            if study:
                q = q.where(Trial.study == study)
            total = s.scalar(select(func.count()).select_from(q.subquery()))
            items = s.scalars(q.order_by(Trial.id.desc()).limit(limit)).all()
            return {"total": int(total or 0), "items": [
                {"id": t.id, "created_at": _iso(t.created_at), "study": t.study, "level": t.level,
                 "strategy": t.strategy, "group": t.group, "params": t.params,
                 "metrics": {k: v for k, v in (t.metrics or {}).items() if not isinstance(v, (dict, list))}}
                for t in items]}

    @app.get("/api/health")
    def health():
        return {"ok": True, "service": service is not None and getattr(service, "ready", False)}

    # ------------------------------------------------------------------ SPA

    if WEB_DIST.exists():
        @app.get("/{path:path}", include_in_schema=False)
        def spa(path: str):
            if path.startswith(("api/", "ws/")):
                raise HTTPException(404)
            f = WEB_DIST / path
            if path and f.is_file() and WEB_DIST in f.resolve().parents:
                return FileResponse(f)
            return FileResponse(WEB_DIST / "index.html")

    return app


_LLM_FACTS: dict[str, dict] = {}


def _json_key(facts: dict) -> str:
    import json

    key = json.dumps(facts, sort_keys=True, ensure_ascii=False, default=str)
    _LLM_FACTS[key] = facts
    return key


@lru_cache(maxsize=512)
def _llm_cached(sym: str, tf: str, ts: str, key: str) -> str | None:
    """Резюме считается один раз на (символ, ТФ, бар, набор фактов)."""
    from quant.analysis.llm import summarize

    return summarize(_LLM_FACTS.pop(key, {}))


@lru_cache(maxsize=2)
def _oos_trades_cached(strategy: str, key: str) -> pl.DataFrame | None:
    p = Path(key) / "oos" / "trades.parquet"
    return pl.read_parquet(p) if p.exists() else None


def _oos_trades(strategy: str) -> pl.DataFrame | None:
    with session_scope() as s:
        r = s.scalars(select(BacktestRun).where(BacktestRun.kind == "walkforward", BacktestRun.strategy == strategy)
                      .order_by(BacktestRun.id.desc()).limit(1)).first()
        if r is None or not r.artifacts_dir:
            return None
        key = r.artifacts_dir
    return _oos_trades_cached(strategy, key)
