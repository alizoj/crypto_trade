"""Журнал сделок и дисциплина из книг (docs/Books).

- Элдер «Трейдинг с доктором Элдером», гл. 8: таблица всех сделок и дневник (причина входа/выхода — заметка);
  гл. 7: правило 6% — убыток с начала месяца плюс риск открытых позиций не больше 6% баланса на начало месяца,
  иначе новые сделки до конца месяца не открываются.
- Дуглас «Зональный трейдинг», гл. 11: оценивать торговлю выборками по 20 сделок, а не по последней сделке;
  следить за своими ошибками — сделки вне плана, без стопа, убыток больше заранее принятого риска.

Месяц — по проп-календарю рынка (день начинается в day_boundary, для Crypto+TradFi 00:00 UTC+4).
Результат месяца = баланс − баланс на начало месяца: в него входят и комиссии открытых позиций,
и ручные правки баланса (баланс вводится с дашборда HashHedge).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from quant.config import load_challenge_config
from quant.db.models import Account as AccountRow
from quant.db.models import AccountMonth, JournalTrade, LiveSignal, ManualPosition
from quant.timeutil import MIN_PER_DAY, from_min, now_utc, to_min

EXIT_REASONS = ("manual", "sl", "tp", "timeout", "other")
SERIES_SIZE = 20                  # Дуглас: выборка из 20 сделок
STOP_BREACH_R = -1.1              # убыток больше принятого риска на 10%+ — стоп не соблюдён (перенесён/не стоял)


def month_key(now: datetime, boundary_min: int) -> str:
    """Месяц проп-календаря: сдвиг на (сутки − граница дня), т.е. для границы 20:00 UTC — время UTC+4."""
    local = from_min(to_min(now) + (MIN_PER_DAY - boundary_min) % MIN_PER_DAY)
    return f"{local.year:04d}-{local.month:02d}"


def _boundary(row: AccountRow) -> int:
    return load_challenge_config().market(row.market).day_boundary_min


def ensure_month(s: Session, row: AccountRow, now: datetime | None = None) -> AccountMonth:
    """Запись месяца для счёта; нет — создаётся с текущим балансом (зовётся каждую минуту из roll_day,
    поэтому при смене месяца баланс берётся в первую минуту нового месяца)."""
    key = month_key(now or now_utc(), _boundary(row))
    if row.id is None:
        s.flush()
    m = s.scalar(select(AccountMonth).where(AccountMonth.account_id == row.id, AccountMonth.month == key))
    if m is None:
        m = AccountMonth(account=row, month=key, start_balance=row.balance)
        s.add(m)
        s.flush()
    return m


def _position_risk(p: ManualPosition, marks: dict[str, float]) -> float:
    """Риск открытой позиции: до стопа; без стопа — текущий нереализованный убыток (риск не ограничен)."""
    if p.sl is not None:
        return p.qty * abs(p.entry_price - p.sl)
    side = 1 if p.side == "long" else -1
    mark = marks.get(p.hh_symbol, p.entry_price)
    return max(0.0, -side * p.qty * (mark - p.entry_price))


def month_status(s: Session, row: AccountRow, marks: dict[str, float], pct: float,
                 now: datetime | None = None) -> dict[str, Any]:
    """Правило 6% Элдера: сколько месячного лимита уже занято убытком и риском открытых позиций."""
    m = ensure_month(s, row, now)
    realized = row.balance - m.start_balance
    open_risk = sum(_position_risk(p, marks) for p in row.positions)
    limit = pct * m.start_balance
    used = max(0.0, -realized) + open_risk
    enabled = pct > 0
    return {"month": m.month, "start_balance": m.start_balance, "result_usd": realized, "open_risk_usd": open_risk,
            "limit_pct": pct, "limit_usd": limit, "used_usd": used, "room_usd": limit - used if enabled else None,
            "enabled": enabled, "blocked": enabled and used >= limit}


def month_room(s: Session, row: AccountRow, marks: dict[str, float], pct: float) -> float | None:
    """Свободный месячный лимит для risk governor (None — правило выключено)."""
    return month_status(s, row, marks, pct)["room_usd"] if pct > 0 else None


def record_close(s: Session, row: AccountRow, pos: ManualPosition, exit_price: float, *, fee_rate: float,
                 reason: str = "manual", note: str = "", now: datetime | None = None) -> JournalTrade:
    """Запись закрытой сделки. Комиссия входа — та же ставка taker, что списана при открытии (add_position)."""
    if reason not in EXIT_REASONS:
        raise ValueError(f"причина выхода {reason!r}, есть {EXIT_REASONS}")
    side = 1 if pos.side == "long" else -1
    fees = pos.qty * (pos.entry_price + exit_price) * fee_rate
    pnl = side * pos.qty * (exit_price - pos.entry_price) - fees
    risk = pos.qty * abs(pos.entry_price - pos.sl) if pos.sl is not None else None
    strategy = ""
    if pos.signal_id is not None:
        sig = s.get(LiveSignal, pos.signal_id)
        strategy = sig.strategy if sig is not None else ""
    t = JournalTrade(account=row, hh_symbol=pos.hh_symbol, side=pos.side, qty=pos.qty, entry_price=pos.entry_price,
                     exit_price=exit_price, sl=pos.sl, tp=pos.tp, opened_at=pos.opened_at,
                     closed_at=now or now_utc(), signal_id=pos.signal_id, strategy=strategy, fees_usd=fees,
                     pnl_usd=pnl, risk_usd=risk, r_multiple=pnl / risk if risk else None, exit_reason=reason,
                     note=note or "")
    s.add(t)
    return t


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def trade_json(t: JournalTrade) -> dict[str, Any]:
    return {"id": t.id, "account_id": t.account_id, "hh_symbol": t.hh_symbol, "side": t.side, "qty": t.qty,
            "entry_price": t.entry_price, "exit_price": t.exit_price, "sl": t.sl, "tp": t.tp,
            "opened_at": _iso(t.opened_at), "closed_at": _iso(t.closed_at), "signal_id": t.signal_id,
            "strategy": t.strategy, "fees_usd": t.fees_usd, "pnl_usd": t.pnl_usd, "risk_usd": t.risk_usd,
            "r_multiple": t.r_multiple, "exit_reason": t.exit_reason, "note": t.note}


def _block(trades: list[JournalTrade], expected: dict[int, float]) -> dict[str, Any]:
    """Метрики блока сделок (серии Дугласа или всего журнала)."""
    n = len(trades)
    rs = [t.r_multiple for t in trades if t.r_multiple is not None]
    wins = sum(1 for t in trades if t.pnl_usd > 0)
    gain = sum(t.pnl_usd for t in trades if t.pnl_usd > 0)
    loss = -sum(t.pnl_usd for t in trades if t.pnl_usd < 0)
    streak = worst = 0
    for t in trades:
        streak = streak + 1 if t.pnl_usd < 0 else 0
        worst = max(worst, streak)
    exp = [expected[t.signal_id] for t in trades if t.signal_id is not None and t.signal_id in expected]
    win_r = [r for r in rs if r > 0]
    loss_r = [r for r in rs if r <= 0]
    return {
        "n": n, "wins": wins, "win_rate": wins / n if n else None,
        "pnl_usd": sum(t.pnl_usd for t in trades), "fees_usd": sum(t.fees_usd for t in trades),
        "n_r": len(rs), "sum_r": sum(rs) if rs else None, "avg_r": sum(rs) / len(rs) if rs else None,
        "avg_win_r": sum(win_r) / len(win_r) if win_r else None, "avg_loss_r": sum(loss_r) / len(loss_r) if loss_r else None,
        "profit_factor": gain / loss if loss > 0 else None, "max_loss_streak": worst,
        "expected_avg_r": sum(exp) / len(exp) if exp else None, "n_expected": len(exp),
        # дисциплина (Дуглас: «я постоянно наблюдаю за своими ошибками»)
        "from_signals": sum(1 for t in trades if t.signal_id is not None),
        "without_stop": sum(1 for t in trades if t.sl is None),
        "stop_breaches": sum(1 for t in trades if t.r_multiple is not None and t.r_multiple < STOP_BREACH_R),
        "first_closed_at": _iso(trades[0].closed_at) if trades else None,
        "last_closed_at": _iso(trades[-1].closed_at) if trades else None,
    }


def journal_stats(trades: Iterable[JournalTrade], *, expected: dict[int, float] | None = None,
                  size: int = SERIES_SIZE) -> dict[str, Any]:
    """Итоги журнала и серии по size сделок в порядке закрытия; последняя серия может быть незавершённой."""
    ts = sorted(trades, key=lambda t: (t.closed_at, t.id or 0))
    exp = expected or {}
    series = []
    for k in range(0, len(ts), size):
        chunk = ts[k:k + size]
        series.append({"index": k // size + 1, "complete": len(chunk) == size, **_block(chunk, exp)})
    return {"series_size": size, "total": _block(ts, exp), "series": series}


def _r(x: float | None) -> str:
    return "—" if x is None else f"{x:+.2f}R"


def format_stats(st: dict[str, Any]) -> str:
    """Текстовая сводка журнала (бот): текущая серия Дугласа, прошлые серии, итог."""
    tot = st["total"]
    if not tot["n"]:
        return "Журнал пуст: закрытые позиции появятся здесь (закрытие — /pos close или в интерфейсе)."
    size = st["series_size"]
    cur = st["series"][-1]
    lines = [f"Серия {cur['index']}: {cur['n']}/{size} сделок" + ("" if cur["complete"] else " — вывод после 20 (Дуглас)"),
             f"  прибыльных {cur['wins']}/{cur['n']}, средний {_r(cur['avg_r'])}, итог {cur['pnl_usd']:+,.2f}$, "
             f"убытков подряд макс. {cur['max_loss_streak']}"]
    if cur["expected_avg_r"] is not None:
        lines.append(f"  ожидание по сигналам {_r(cur['expected_avg_r'])} ({cur['n_expected']} сд.)")
    lines.append(f"  дисциплина: по сигналам {cur['from_signals']}/{cur['n']}, без стопа {cur['without_stop']}, "
                 f"убыток больше риска {cur['stop_breaches']}")
    for sr in st["series"][:-1][-3:]:
        lines.append(f"Серия {sr['index']}: средний {_r(sr['avg_r'])}, прибыльных {sr['wins']}/{sr['n']}, {sr['pnl_usd']:+,.2f}$")
    pf = f"{tot['profit_factor']:.2f}" if tot["profit_factor"] is not None else "—"
    lines.append(f"Всего {tot['n']} сделок: {tot['pnl_usd']:+,.2f}$, средний {_r(tot['avg_r'])}, PF {pf}")
    return "\n".join(lines)


def expected_r(s: Session, trades: Iterable[JournalTrade]) -> dict[int, float]:
    """Ожидание в R из сигналов (ev_r при калиброванной вероятности) — для сравнения серии с планом."""
    ids = {t.signal_id for t in trades if t.signal_id is not None}
    if not ids:
        return {}
    rows = s.execute(select(LiveSignal.id, LiveSignal.ev_r).where(LiveSignal.id.in_(ids))).all()
    return {i: float(ev) for i, ev in rows if ev is not None}
