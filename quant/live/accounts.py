"""Счета пользователя в live: состояние из БД (ручной ввод) → propsim.Account с текущими mark-ценами.

Используется API (экран 3, шапка скринера), сервисом сигналов (план сделки) и ботом (/balance, /pos).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from quant.config import load_challenge_config
from quant.db.models import Account as AccountRow
from quant.db.models import ManualPosition
from quant.propsim import Account, Position
from quant.timeutil import minute_of_day, now_utc, prop_day_id, to_min


def to_propsim(row: AccountRow, marks: dict[str, float], now: datetime | None = None) -> Account:
    cfg = load_challenge_config()
    now_m = to_min(now or now_utc())
    acc = Account.new(cfg, challenge_type=row.challenge_type, stage=row.stage, size=row.size, start_ts=now_m,
                      market=row.market, balance=row.balance)
    acc.day_start_balance = row.start_of_day_balance
    acc.trading_days = set(int(d) for d in (row.trading_days or []))
    acc.dlp_available = bool(row.dlp_available)
    for p in row.positions:
        side = 1 if p.side == "long" else -1
        acc.positions[p.id] = Position(id=p.id, symbol=p.hh_symbol, side=side, qty=p.qty, entry_price=p.entry_price,
                                       mark=float(marks.get(p.hh_symbol, p.entry_price)), opened_ts=to_min(p.opened_at))
    tb = acc.target_balance
    acc.target_reached = bool(tb is not None and row.balance >= tb)
    if row.status != "active":
        acc.status = row.status  # type: ignore[assignment]
    return acc


def account_json(row: AccountRow, marks: dict[str, float], soft_stop: float = 0.6) -> dict[str, Any]:
    acc = to_propsim(row, marks)
    tb = acc.target_balance
    eq = acc.equity()
    positions = []
    for p in row.positions:
        m = marks.get(p.hh_symbol)
        side = 1 if p.side == "long" else -1
        positions.append({
            "id": p.id, "hh_symbol": p.hh_symbol, "side": p.side, "qty": p.qty, "entry_price": p.entry_price,
            "notional_usd": p.qty * (m or p.entry_price), "mark_price": m,
            "unrealized_usd": side * p.qty * (m - p.entry_price) if m is not None else None,
            "opened_at": _iso(p.opened_at), "sl": p.sl, "tp": p.tp})
    daily_loss = eq - acc.day_start_balance
    return {
        "id": row.id, "name": row.name, "challenge_type": row.challenge_type, "market": row.market, "size": row.size,
        "stage": row.stage, "balance": row.balance, "equity": eq, "start_of_day_balance": acc.day_start_balance,
        "daily_floor": acc.daily_floor, "total_floor": acc.total_floor, "daily_limit_usd": acc.daily_limit_usd,
        "total_limit_usd": acc.max_loss_usd, "daily_room_usd": acc.daily_room(),
        "total_room_usd": acc.total_room(), "target_usd": tb,
        "progress_pct": ((row.balance - row.size) / (tb - row.size) * 100) if tb else None,
        "trading_days": acc.trading_days_count, "min_trading_days": acc.min_trading_days,
        "soft_stop_hit": (-daily_loss) >= soft_stop * acc.daily_limit_usd,
        "target_lock": acc.target_reached and acc.trading_days_count < acc.min_trading_days,
        "status": row.status, "positions": positions, "updated_at": _iso(row.updated_at),
    }


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def list_accounts(s: Session, *, active_only: bool = False) -> list[AccountRow]:
    q = select(AccountRow).options(selectinload(AccountRow.positions)).order_by(AccountRow.id)
    if active_only:
        q = q.where(AccountRow.status == "active")
    return list(s.scalars(q).all())


def create_account(s: Session, *, name: str, challenge_type: str, size: float, stage: str = "stage1",
                   market: str = "crypto_tradfi") -> AccountRow:
    cfg = load_challenge_config()
    if challenge_type not in cfg.challenges:
        raise ValueError(f"неизвестный тип челленджа {challenge_type}")
    row = AccountRow(name=name, challenge_type=challenge_type, market=market, size=size, stage=stage, balance=size,
                     start_of_day_balance=size, start_of_day_at=now_utc(), peak_equity=size, trading_days=[],
                     status="active", dlp_available=cfg.options.daily_loss_protection)
    s.add(row)
    s.flush()
    return row


def add_position(s: Session, row: AccountRow, *, hh_symbol: str, side: str, qty: float, entry_price: float,
                 sl: float | None = None, tp: float | None = None, signal_id: int | None = None) -> ManualPosition:
    cfg = load_challenge_config()
    p = ManualPosition(account=row, hh_symbol=hh_symbol, side=side, qty=qty, entry_price=entry_price, sl=sl, tp=tp,
                       signal_id=signal_id, opened_at=now_utc())
    s.add(p)
    # торговый день и комиссия открытия
    boundary = cfg.market(row.market).day_boundary_min
    d = prop_day_id(to_min(now_utc()), boundary)
    days = set(row.trading_days or [])
    days.add(d)
    row.trading_days = sorted(days)
    row.balance -= qty * entry_price * cfg.market(row.market).fees.taker
    s.flush()
    return p


def close_position(s: Session, row: AccountRow, pos_id: int, exit_price: float) -> None:
    cfg = load_challenge_config()
    p = next(p for p in row.positions if p.id == pos_id)
    side = 1 if p.side == "long" else -1
    fee = p.qty * exit_price * cfg.market(row.market).fees.taker
    row.balance += side * p.qty * (exit_price - p.entry_price) - fee
    row.positions.remove(p)
    s.delete(p)
    s.flush()


def roll_day(s: Session, now: datetime | None = None) -> int:
    """После окна сброса: зафиксировать баланс начала дня (ручной ввод баланса)  # ASSUMPTION."""
    now = now or now_utc()
    cfg = load_challenge_config()
    n = 0
    for row in list_accounts(s, active_only=True):
        mk = cfg.market(row.market)
        m = to_min(now)
        if minute_of_day(m) < mk.day_boundary_min + mk.reset_window_minutes and minute_of_day(m) >= mk.day_boundary_min:
            continue
        last = row.start_of_day_at
        if last is None or prop_day_id(to_min(last), mk.day_boundary_min) != prop_day_id(m, mk.day_boundary_min):
            row.start_of_day_balance = row.balance
            row.start_of_day_at = now
            n += 1
    return n
