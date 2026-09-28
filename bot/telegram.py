"""Telegram-бот (раздел 8.4): сигналы, служебные алерты, команды управления счётом.

- доступ только для whitelist chat_id; приватный канал — только торговые сигналы без данных счёта;
- при смене статуса сигнала исходное сообщение редактируется, итог — ответом (reply);
- кнопки «Взял» / «Пропустил» / «Открыть в интерфейсе»; нажатия пишутся в журнал;
- тихие часы: сигналы не отправляются (алерты — отправляются).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from quant.analysis.text import fmt_price
from quant.bus import Bus
from quant.backtest.challenge import RiskParams
from quant.config import Settings, get_settings, load_challenge_config, load_strategy_config
from quant.db import session_scope
from quant.db.models import Account as AccountRow
from quant.db.models import LiveSignal
from quant.live.accounts import account_json, add_position, close_position, list_accounts
from quant.live.journal import EXIT_REASONS, format_stats
from quant.timeutil import next_reset_min, now_utc, to_min

log = logging.getLogger(__name__)

STATUS_TEXT = {"active": "ожидает входа", "triggered": "вход сработал", "expired": "истёк без входа",
               "invalidated": "отменён: цена ушла", "tp": "✅ TP", "sl": "❌ SL", "timeout": "⏱ таймаут"}
FINAL = ("expired", "invalidated", "tp", "sl", "timeout")


def _pct(x: float | None) -> str:
    return "—" if x is None else f"{x * 100:.0f}%"


def _usd(x: float | None) -> str:
    return "—" if x is None else f"${x:,.0f}".replace(",", " ")


def _hm(minutes: float) -> str:
    m = int(max(0, minutes))
    return f"{m // 60}ч {m % 60:02d}м"


def _month_line(m: dict | None) -> str:
    """Правило 6% Элдера одной строкой для /status."""
    if not m:
        return ""
    res = f"\n  месяц {m['month']}: {m['result_usd']:+,.0f}$"
    if m["enabled"]:
        res += f", занято {m['used_usd']:,.0f} из {m['limit_usd']:,.0f}$ (лимит {m['limit_pct'] * 100:.0f}%)"
        if m["blocked"]:
            res += " · ЛИМИТ МЕСЯЦА — без новых сделок"
    return res


def format_signal(sig: dict, *, now: datetime | None = None, with_account: bool = True) -> str:
    now = now or now_utc()
    side = sig["side"]
    head = f"{'🟢 LONG' if side == 'long' else '🔴 SHORT'} {sig['hh_symbol']} · {sig['tf'].upper()} · score {sig['score']:.0f}"
    p = sig.get("prob") or {}
    if p.get("p") is not None and p.get("calibrated"):
        prob = (f"Вероятность: {_pct(p['p'])} (ДИ {p['ci_low'] * 100:.0f}–{p['ci_high'] * 100:.0f}%, n={p['n']})"
                + (f" · EV {sig['ev_r']:+.2f}R" if sig.get("ev_r") is not None else ""))
    else:
        prob = "Вероятность: не откалибровано (показан только score)"
    plan = sig.get("plan") or {}
    rr = f"{plan['rr1']:.1f}" if plan.get("rr1") else "—"
    if plan.get("rr2"):
        rr += f"/{plan['rr2']:.1f}"
    entry = (f"Вход: {fmt_price(sig['entry'])} {sig['entry_type']} · SL {fmt_price(sig['sl'])} "
             f"(stop-market, {sig.get('sl_trigger', 'last')}) · TP1 {fmt_price(sig['tp1'])}"
             + (f" · TP2 {fmt_price(sig['tp2'])}" if sig.get("tp2") else "") + f" · R:R {rr}")
    lines = [head, prob, entry]
    if with_account and plan.get("notional_usd"):
        room = plan.get("risk_pct_daily_room")
        lines.append(f"Размер: {_usd(plan['notional_usd'])} notional · риск {_usd(plan.get('risk_usd'))}"
                     + (f" ({room * 100:.0f}% дневного запаса)" if room else ""))
    elif with_account and plan.get("blocked_reason"):
        lines.append(f"Размер: не рассчитан — {plan['blocked_reason']}")
    reason = sig.get("reason") or {}
    if reason.get("pros"):
        lines.append("За: " + ", ".join(reason["pros"]))
    if reason.get("cons"):
        lines.append("Против: " + "; ".join(reason["cons"]))
    exp = sig.get("expires_at")
    tail = []
    if exp:
        dt = datetime.fromisoformat(exp.replace("Z", "+00:00"))
        tail.append(f"Действует до {dt:%H:%M} UTC")
    boundary = load_challenge_config().market("crypto_tradfi").day_boundary_min
    tail.append(f"до ресета лимита {_hm(next_reset_min(to_min(now), boundary) - to_min(now))}")
    lines.append(" · ".join(tail))
    st = sig.get("status")
    if st and st not in ("new", "active"):
        lines.append(f"Статус: {STATUS_TEXT.get(st, st)}"
                     + (f" ({sig['outcome_r']:+.2f}R)" if sig.get("outcome_r") is not None else ""))
    return "\n".join(lines)


def in_quiet_hours(spec: str, now: datetime) -> bool:
    if not spec:
        return False
    a, b = spec.split("-")
    ma = int(a[:2]) * 60 + int(a[3:])
    mb = int(b[:2]) * 60 + int(b[3:])
    m = now.hour * 60 + now.minute
    return ma <= m < mb if ma < mb else (m >= ma or m < mb)


class TelegramBot:
    def __init__(self, bus: Bus, settings: Settings | None = None, strategy: str = "confluence_v1"):
        from aiogram import Bot, Dispatcher, F, Router
        from aiogram.filters import Command

        self.s = settings or get_settings()
        self.monthly_loss_pct = RiskParams.from_config(load_strategy_config(strategy).get("risk")).monthly_loss_pct
        if not self.s.telegram_token:
            raise RuntimeError("QUANT_TELEGRAM_TOKEN не задан")
        self.bus = bus
        self.bot = Bot(self.s.telegram_token)
        self.dp = Dispatcher()
        self.chats = self.s.chat_ids
        r = Router()
        r.message.filter(F.chat.id.in_(self.chats))
        r.callback_query.filter(F.message.chat.id.in_(self.chats))
        r.message(Command("start"))(self.cmd_start)
        r.message(Command("status"))(self.cmd_status)
        r.message(Command("balance"))(self.cmd_balance)
        r.message(Command("equity"))(self.cmd_status)
        r.message(Command("pos"))(self.cmd_pos)
        r.message(Command("signals"))(self.cmd_signals)
        r.message(Command("journal"))(self.cmd_journal)
        r.callback_query(F.data.startswith("take:") | F.data.startswith("skip:"))(self.on_decision)
        self.dp.include_router(r)

    async def run(self) -> None:
        await asyncio.gather(self.dp.start_polling(self.bot, handle_signals=False), self._signals(), self._alerts())

    # ------------------------------------------------------------------ исходящие

    def _keyboard(self, sig_id: int, sym: str, tf: str):  # noqa: ANN202
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        url = f"{self.s.web_base_url.rstrip('/')}/instrument/{sym}?tf={tf}&signal={sig_id}"
        buttons = [InlineKeyboardButton(text="Взял", callback_data=f"take:{sig_id}"),
                   InlineKeyboardButton(text="Пропустил", callback_data=f"skip:{sig_id}")]
        row2 = [InlineKeyboardButton(text="Открыть в интерфейсе", url=url)] if url.startswith("https://") else []
        return InlineKeyboardMarkup(inline_keyboard=[buttons] + ([row2] if row2 else []))

    async def _signals(self) -> None:
        async for msg in self.bus.subscribe("signals", group="bot"):
            try:
                sig = msg.get("signal") or {}
                if msg.get("type") == "new":
                    await self._send_new(sig)
                elif msg.get("type") == "status":
                    await self._on_status(sig)
            except Exception:
                log.exception("telegram: сигнал")

    async def _send_new(self, sig: dict) -> None:
        if in_quiet_hours(self.s.quiet_hours_utc, now_utc()):
            return
        ids = {}
        for chat in self.chats:
            m = await self.bot.send_message(chat, format_signal(sig), reply_markup=self._keyboard(sig["id"], sig["hh_symbol"], sig["tf"]))
            ids[str(chat)] = m.message_id
            await asyncio.sleep(0.05)
        if self.s.telegram_channel_id:
            m = await self.bot.send_message(self.s.telegram_channel_id, format_signal(sig, with_account=False))
            ids[str(self.s.telegram_channel_id)] = m.message_id
        with session_scope() as s:
            row = s.get(LiveSignal, sig["id"])
            if row is not None:
                row.tg_message_ids = ids

    async def _on_status(self, sig: dict) -> None:
        with session_scope() as s:
            row = s.get(LiveSignal, sig["id"])
            ids = dict(row.tg_message_ids or {}) if row else {}
        for chat, mid in ids.items():
            chan = chat == str(self.s.telegram_channel_id)
            try:
                await self.bot.edit_message_text(format_signal(sig, with_account=not chan), chat_id=chat, message_id=mid,
                                                 reply_markup=None if chan or sig["status"] in FINAL
                                                 else self._keyboard(sig["id"], sig["hh_symbol"], sig["tf"]))
                if sig["status"] in FINAL:
                    await self.bot.send_message(chat, f"{sig['hh_symbol']} {sig['side']}: {STATUS_TEXT[sig['status']]}"
                                                + (f" {sig['outcome_r']:+.2f}R" if sig.get("outcome_r") is not None else ""),
                                                reply_to_message_id=mid)
            except Exception as e:  # сообщение могло быть удалено
                log.warning("telegram edit %s/%s: %s", chat, mid, e)

    async def _alerts(self) -> None:
        async for a in self.bus.subscribe("alerts", group="bot-alerts"):
            for chat in self.chats:
                try:
                    await self.bot.send_message(chat, f"⚠️ {a.get('message')}")
                except Exception:
                    log.exception("telegram alert")

    # ------------------------------------------------------------------ команды

    async def cmd_start(self, message) -> None:  # noqa: ANN001
        await message.answer("Бот HashHedge Quant. Команды:\n/status — счета\n/balance <id> <баланс>\n"
                             "/pos add <id> <SYMBOL> <long|short> <qty> <price> [sl] [tp]\n"
                             "/pos close <id> <pos_id> <price> [sl|tp|manual|timeout|other] [заметка]\n"
                             "/journal [id] — журнал сделок: серии по 20, итоги, правило 6%\n"
                             "/signals — активные сигналы")

    async def cmd_status(self, message) -> None:  # noqa: ANN001
        with session_scope() as s:
            rows = [account_json(a, {}, monthly_loss_pct=self.monthly_loss_pct) for a in list_accounts(s)]
        if not rows:
            await message.answer("Счетов нет — добавьте в интерфейсе.")
            return
        lines = []
        for a in rows:
            lines.append(f"#{a['id']} {a['name']} ({a['stage']}, ${a['size']:,.0f}): баланс {a['balance']:,.2f}, "
                         f"equity {a['equity']:,.2f}\n  запас: дневной {a['daily_room_usd']:,.0f}$, общий {a['total_room_usd']:,.0f}$ · "
                         f"дней {a['trading_days']}/{a['min_trading_days']}"
                         + (" · SOFT STOP" if a["soft_stop_hit"] else "") + (" · добор дней" if a["target_lock"] else "")
                         + _month_line(a.get("month")))
        await message.answer("\n".join(lines))

    async def cmd_balance(self, message) -> None:  # noqa: ANN001
        parts = (message.text or "").split()
        if len(parts) != 3:
            await message.answer("Формат: /balance <id> <баланс>")
            return
        with session_scope() as s:
            row = s.get(AccountRow, int(parts[1]))
            if row is None:
                await message.answer("Нет такого счёта")
                return
            row.balance = float(parts[2])
        await message.answer("Баланс обновлён.")
        await self.cmd_status(message)

    async def cmd_pos(self, message) -> None:  # noqa: ANN001
        from sqlalchemy.orm import selectinload

        p = (message.text or "").split()
        try:
            with session_scope() as s:
                if len(p) >= 7 and p[1] == "add":
                    row = s.get(AccountRow, int(p[2]), options=[selectinload(AccountRow.positions)])
                    add_position(s, row, hh_symbol=p[3].upper(), side=p[4], qty=float(p[5]), entry_price=float(p[6]),
                                 sl=float(p[7]) if len(p) > 7 else None, tp=float(p[8]) if len(p) > 8 else None)
                elif len(p) >= 5 and p[1] == "close":
                    row = s.get(AccountRow, int(p[2]), options=[selectinload(AccountRow.positions)])
                    has_reason = len(p) > 5 and p[5] in EXIT_REASONS
                    close_position(s, row, int(p[3]), float(p[4]), reason=p[5] if has_reason else "manual",
                                   note=" ".join(p[6:] if has_reason else p[5:]))
                else:
                    await message.answer("Формат: /pos add <id> <SYMBOL> <long|short> <qty> <price> [sl] [tp] | "
                                         "/pos close <id> <pos_id> <price> [sl|tp|manual|timeout|other] [заметка]")
                    return
        except Exception as e:
            await message.answer(f"Ошибка: {e}")
            return
        await self.cmd_status(message)

    async def cmd_signals(self, message) -> None:  # noqa: ANN001
        from sqlalchemy import select

        with session_scope() as s:
            rows = s.scalars(select(LiveSignal).where(LiveSignal.status.in_(("new", "active", "triggered")))
                             .order_by(LiveSignal.id.desc()).limit(20)).all()
            text = "\n".join(f"#{r.id} {r.hh_symbol} {r.side} {r.tf} score {r.score:.0f} — {STATUS_TEXT.get(r.status, r.status)}"
                             for r in rows) or "Активных сигналов нет."
        await message.answer(text)

    async def cmd_journal(self, message) -> None:  # noqa: ANN001
        from sqlalchemy import select

        from quant.db.models import JournalTrade
        from quant.live.journal import expected_r, journal_stats

        p = (message.text or "").split()
        with session_scope() as s:
            q = select(JournalTrade)
            if len(p) > 1 and p[1].isdigit():
                q = q.where(JournalTrade.account_id == int(p[1]))
            trades = s.scalars(q).all()
            text = format_stats(journal_stats(trades, expected=expected_r(s, trades)))
            last = sorted(trades, key=lambda t: (t.closed_at, t.id))[-5:]
            if last:
                text += "\nПоследние сделки:\n" + "\n".join(
                    f"  {t.hh_symbol} {t.side} {t.pnl_usd:+,.2f}$"
                    + (f" ({t.r_multiple:+.2f}R)" if t.r_multiple is not None else "") + f" · {t.exit_reason}"
                    for t in reversed(last))
        await message.answer(text)

    async def on_decision(self, cb) -> None:  # noqa: ANN001
        kind, sid = cb.data.split(":")
        with session_scope() as s:
            row = s.get(LiveSignal, int(sid))
            if row is not None:
                row.taken = kind == "take"
        await cb.answer("Записано: " + ("взял" if kind == "take" else "пропустил"))
