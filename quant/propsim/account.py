"""Движок правил HashHedge (раздел 2 ТЗ): эталонная событийная модель счёта.

Используется в unit-тестах 2.6, в live (учёт ручных позиций) и как эталон для быстрого
numba-движка бэктеста (перекрёстная проверка в tests/backtest).

Ключевые правила:
- equity = balance + unrealized PnL по mark price (раздел 2.2);
- daily_floor = balance_at_day_start − daily_limit_usd (лимит фиксирован от размера счёта);
- провал: equity <= floor в любой момент; позиции закрываются по цене, соответствующей floor, не лучше;
- граница дня — окно (reset_window_mode: new_day | old_day | both).
Время — целые минуты с эпохи (quant.timeutil.to_min).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from itertools import count
from typing import Literal

from quant.config import ChallengeConfig, MarketRules, StageSpec
from quant.timeutil import MIN_PER_DAY, minute_of_day, prop_day_id, prop_day_start_min, to_min

Status = Literal["active", "passed", "failed"]
EPS = 1e-9


class OrderRejected(Exception):
    """Ордер не проходит проверки (плечо, max_per_lot, max_total, режим, блокировка)."""


class AccountClosed(Exception):
    """Операция над счётом, который уже прошёл или провалил стадию."""


@dataclass
class Position:
    id: int
    symbol: str
    side: int                    # +1 long, -1 short
    qty: float                   # в базовом активе (нормализовано)
    entry_price: float
    mark: float
    opened_ts: int
    last: float | None = None    # последняя last-цена (для закрытия по рынку)

    @property
    def notional_entry(self) -> float:
        return self.qty * self.entry_price

    @property
    def notional_mark(self) -> float:
        return self.qty * self.mark

    def unrealized(self, price: float | None = None) -> float:
        p = self.mark if price is None else price
        return self.side * self.qty * (p - self.entry_price)


@dataclass
class Event:
    ts: int
    kind: str
    data: dict = field(default_factory=dict)


def _ts(value: int | datetime) -> int:
    return to_min(value) if isinstance(value, datetime) else int(value)


class Account:
    """Счёт одной стадии челленджа."""

    def __init__(self, cfg: ChallengeConfig, *, challenge_type: str, stage: str, size: float,
                 start_ts: int, market: str = "crypto_tradfi", balance: float | None = None,
                 limits: dict[str, tuple[float, float]] | None = None):
        self.cfg = cfg
        self.opts = cfg.options
        self.challenge_type = challenge_type
        self.stage = stage
        self.market_name = market
        self.market: MarketRules = cfg.market(market)
        self.spec: StageSpec = cfg.stage(challenge_type, stage)
        self.size = float(size)
        self.balance = float(balance if balance is not None else size)
        self.positions: dict[int, Position] = {}
        self.events: list[Event] = []
        self.status: Status = "active"
        self.fail_reason: str | None = None
        self.trading_days: set[int] = set()
        self.dlp_available = bool(self.opts.daily_loss_protection)
        self.dlr_available = bool(self.opts.daily_loss_reset) and stage != "trader"
        self._ids = count(1)
        self._limits = limits           # {symbol: (max_per_lot, max_total)}; None → из instruments.csv
        self.boundary = self.market.day_boundary_min
        self.window = self.market.reset_window_minutes
        self.now = _ts(start_ts)
        self.day_id = prop_day_id(self.now, self.boundary)
        self.day_start_balance = self.balance
        self.peak = self.balance
        # Окно сброса: floor прошлого дня, который ещё действует, и отложенная фиксация
        self._old_floor: float | None = None
        self._old_floor_until: int | None = None
        self._pending_snapshot_at: int | None = None
        self.target_reached = False
        self.passed_ts: int | None = None
        self.failed_ts: int | None = None

    # ------------------------------------------------------------------ фабрика

    @classmethod
    def new(cls, cfg: ChallengeConfig, *, challenge_type: str, stage: str, size: float, start_ts: int | datetime,
            market: str = "crypto_tradfi", balance: float | None = None,
            limits: dict[str, tuple[float, float]] | None = None) -> "Account":
        return cls(cfg, challenge_type=challenge_type, stage=stage, size=size, start_ts=_ts(start_ts),
                   market=market, balance=balance, limits=limits)

    # ------------------------------------------------------------------ производные величины

    @property
    def daily_limit_usd(self) -> float:
        return self.size * self.spec.daily_loss_pct

    @property
    def max_loss_usd(self) -> float:
        return self.size * self.spec.max_loss_pct

    @property
    def daily_floor(self) -> float:
        """Действующий дневной floor (в окне сброса при old_day/both учитывается и старый)."""
        f = self.day_start_balance - self.daily_limit_usd
        if self._old_floor is not None and self._old_floor_until is not None and self.now < self._old_floor_until:
            f = max(f, self._old_floor)
        return f

    @property
    def total_floor(self) -> float:
        if self.opts.total_floor_mode == "trailing":
            return self.peak - self.max_loss_usd
        return self.size - self.max_loss_usd

    @property
    def target_balance(self) -> float | None:
        if self.spec.target_pct is None:
            return None
        return self.size * (1 + self.spec.target_pct)

    @property
    def min_trading_days(self) -> int:
        return self.spec.min_trading_days

    @property
    def trading_days_count(self) -> int:
        return len(self.trading_days)

    def unrealized(self) -> float:
        return sum(p.unrealized() for p in self.positions.values())

    def equity(self) -> float:
        return self.balance + self.unrealized()

    def daily_loss(self) -> float:
        return self.equity() - self.day_start_balance

    def daily_room(self) -> float:
        return self.equity() - self.daily_floor

    def total_room(self) -> float:
        return self.equity() - self.total_floor

    def gross_notional(self) -> float:
        return sum(p.notional_mark for p in self.positions.values())

    # ------------------------------------------------------------------ время и сброс дня

    def advance(self, ts: int | datetime) -> None:
        """Продвинуть время: сброс дня (окно), автозакрытие US Stocks, завершение окна."""
        ts = _ts(ts)
        if ts < self.now:
            raise ValueError("время не может идти назад")
        while True:
            next_start = prop_day_start_min(self.day_id + 1, self.boundary)
            if next_start > ts:
                break
            self._auto_close_before(next_start)
            self.now = next_start
            self._day_reset(next_start)
        if self._pending_snapshot_at is not None and ts >= self._pending_snapshot_at:
            self.now = max(self.now, self._pending_snapshot_at)
            self._take_snapshot(self._pending_snapshot_at)
        self._auto_close_before(ts + 1)
        self.now = ts

    def _auto_close_before(self, ts: int) -> None:
        """US Stocks: все позиции закрываются автоматически в auto_close_utc."""
        ac = self.market.auto_close_min
        if ac is None or not self.positions or self.status != "active":
            return
        day_start = prop_day_start_min(self.day_id, self.boundary)
        # момент автозакрытия в текущем проп-дне
        t_close = day_start - minute_of_day(day_start) + ac
        if t_close < day_start:
            t_close += MIN_PER_DAY
        if self.now <= t_close < ts or (self.now >= t_close and self.now < ts and t_close >= day_start):
            self.now = max(self.now, t_close)
            for pid in list(self.positions):
                p = self.positions[pid]
                self._close_position(p, p.mark, self.now, liquidity="taker", reason="auto_close")
            self._check(self.now)

    def _day_reset(self, ts: int) -> None:
        mode = self.opts.reset_window_mode
        old_floor = self.day_start_balance - self.daily_limit_usd
        self.day_id = prop_day_id(ts, self.boundary)
        if mode == "old_day":
            self._pending_snapshot_at = ts + self.window
            self._old_floor, self._old_floor_until = None, None
        else:
            self._take_snapshot(ts)
            if mode == "both":
                self._old_floor, self._old_floor_until = old_floor, ts + self.window
            else:
                self._old_floor, self._old_floor_until = None, None
        self.events.append(Event(ts, "day_reset", {"day_id": self.day_id, "mode": mode}))

    def _take_snapshot(self, ts: int) -> None:
        self.day_start_balance = self.balance
        self._pending_snapshot_at = None
        self.events.append(Event(ts, "day_snapshot", {"balance": self.balance}))

    # ------------------------------------------------------------------ лимиты инструментов

    def _instrument_limits(self, symbol: str) -> tuple[float, float] | None:
        if self.market.max_shares_per_ticker is not None:
            return None
        if self._limits is not None and symbol in self._limits:
            return self._limits[symbol]
        from quant.instruments import by_symbol

        inst = by_symbol().get(symbol)
        if inst is None:
            raise OrderRejected(f"unknown_symbol: {symbol}")
        return inst.max_per_lot_usd, inst.max_total_usd

    def _fee(self, notional: float, liquidity: str) -> float:
        rate = self.market.fees.maker if liquidity == "maker" else self.market.fees.taker
        return max(notional * rate, self.market.fees.min_fee_usd)

    # ------------------------------------------------------------------ операции

    def _require_active(self) -> None:
        if self.status != "active":
            raise AccountClosed(f"счёт в статусе {self.status}")

    def open(self, symbol: str, side: Literal["long", "short"] | int, qty: float, price: float,
             ts: int | datetime, *, liquidity: Literal["maker", "taker"] = "taker",
             fee: float | None = None, mark: float | None = None) -> Position:
        self._require_active()
        ts = _ts(ts)
        self.advance(ts)
        self._require_active()
        sgn = side if isinstance(side, int) else (1 if side == "long" else -1)
        if qty <= 0 or price <= 0:
            raise OrderRejected("bad_order: qty и price должны быть > 0")
        notional = qty * price
        # блокировка новых позиций (US Stocks)
        bn = self.market.block_new_min
        if bn is not None:
            m = minute_of_day(ts)
            if m >= bn or m < self.boundary:
                raise OrderRejected("blocked: новые позиции запрещены до следующей сессии")
        same = [p for p in self.positions.values() if p.symbol == symbol]
        if any(p.side != sgn for p in same):
            raise OrderRejected("hedge: встречная позиция по тому же инструменту")  # ASSUMPTION
        if self.market.max_shares_per_ticker is not None:
            if sum(p.qty for p in same) + qty > self.market.max_shares_per_ticker + EPS:
                raise OrderRejected("shares: превышен лимит акций на тикер")
        lim = self._instrument_limits(symbol)
        if lim is not None:
            max_lot, max_total = lim
            if notional > max_lot + EPS:
                raise OrderRejected(f"max_per_lot: {notional:.2f} > {max_lot:.2f}")
            if sum(p.notional_mark for p in same) + notional > max_total + EPS:
                raise OrderRejected(f"max_total: {symbol} > {max_total:.2f}")
        base = self.equity() if self.market.leverage_base == "equity" else self.balance
        cap = self.market.max_leverage * base
        if self.gross_notional() + notional > cap + EPS:
            label = "buying_power" if self.market.leverage_base == "balance" else "leverage"
            raise OrderRejected(f"{label}: {self.gross_notional() + notional:.2f} > {cap:.2f}")
        f = self._fee(notional, liquidity) if fee is None else float(fee)
        pos = Position(id=next(self._ids), symbol=symbol, side=sgn, qty=float(qty), entry_price=float(price),
                       mark=float(mark if mark is not None else price), opened_ts=ts, last=float(price))
        self.positions[pos.id] = pos
        self.balance -= f
        self.trading_days.add(prop_day_id(ts, self.boundary))
        self.events.append(Event(ts, "open", {"id": pos.id, "symbol": symbol, "side": sgn, "qty": qty,
                                              "price": price, "fee": f}))
        self._check(ts)
        return pos

    def close(self, pos_id: int, price: float, ts: int | datetime, *, liquidity: Literal["maker", "taker"] = "taker",
              fee: float | None = None, qty: float | None = None, reason: str = "close") -> float:
        self._require_active()
        ts = _ts(ts)
        self.advance(ts)
        self._require_active()
        pos = self.positions[pos_id]
        pnl = self._close_position(pos, price, ts, liquidity=liquidity, fee=fee, qty=qty, reason=reason)
        self._check(ts)
        return pnl

    def _close_position(self, pos: Position, price: float, ts: int, *, liquidity: str = "taker",
                        fee: float | None = None, qty: float | None = None, reason: str = "close") -> float:
        q = pos.qty if qty is None else min(float(qty), pos.qty)
        pnl = pos.side * q * (price - pos.entry_price)
        f = self._fee(q * price, liquidity) if fee is None else float(fee)
        self.balance += pnl - f
        if q >= pos.qty - EPS:
            del self.positions[pos.id]
        else:
            pos.qty -= q
        self.events.append(Event(ts, reason, {"id": pos.id, "symbol": pos.symbol, "qty": q, "price": price,
                                              "pnl": pnl, "fee": f}))
        return pnl

    def funding(self, symbol: str, rate: float, mark_price: float, ts: int | datetime) -> float:
        """Начисление funding позициям, открытым в момент начисления: qty × mark × rate."""
        self._require_active()
        ts = _ts(ts)
        self.advance(ts)
        self._require_active()
        total = 0.0
        for p in self.positions.values():
            if p.symbol != symbol:
                continue
            p.mark = float(mark_price)
            amount = -p.side * p.qty * mark_price * rate      # лонг платит при rate > 0
            total += amount
        if total:
            self.balance += total
            self.events.append(Event(ts, "funding", {"symbol": symbol, "rate": rate, "amount": total}))
        self._check(ts)
        return total

    def update_marks(self, marks: dict[str, float], ts: int | datetime, lasts: dict[str, float] | None = None) -> None:
        self._require_active()
        ts = _ts(ts)
        self.advance(ts)
        if self.status != "active":
            return
        for p in self.positions.values():
            if p.symbol in marks:
                p.mark = float(marks[p.symbol])
            if lasts and p.symbol in lasts:
                p.last = float(lasts[p.symbol])
        self._check(ts)

    def check_intrabar(self, ts: int | datetime, *, worst_mark: dict[str, tuple[float, float]],
                       worst_last: dict[str, tuple[float, float]] | None = None) -> None:
        """Внутрибарная проверка лимитов по худшей цене (low для лонгов, high для шортов).

        worst_* = {symbol: (low, high)}. Источник цены — options.equity_price_source.
        """
        self._require_active()
        ts = _ts(ts)
        self.advance(ts)
        if self.status != "active" or not self.positions:
            return
        src = worst_mark if self.opts.equity_price_source == "mark" else (worst_last or worst_mark)
        unreal = 0.0
        for p in self.positions.values():
            lo_hi = src.get(p.symbol)
            if lo_hi is None:
                unreal += p.unrealized()
                continue
            worst = lo_hi[0] if p.side > 0 else lo_hi[1]
            unreal += p.unrealized(worst)
        self._check(ts, equity=self.balance + unreal)

    # ------------------------------------------------------------------ проверки

    def _check(self, ts: int, equity: float | None = None) -> None:
        if self.status != "active":
            return
        eq = self.equity() if equity is None else equity
        ref = self.balance if self.opts.trailing_reference == "balance" else eq
        if self.opts.total_floor_mode == "trailing" and equity is None:
            self.peak = max(self.peak, ref)
        total_floor = self.total_floor
        daily_floor = self.daily_floor
        if eq <= total_floor + EPS:
            self._fail(ts, "total", total_floor)
            return
        if eq <= daily_floor + EPS:
            if self.dlp_available:
                self._protect(ts, "daily_loss_protection", daily_floor)
                self.dlp_available = False
                return
            if self.dlr_available:
                self._protect(ts, "daily_loss_reset", daily_floor)
                self.dlr_available = False
                return
            self._fail(ts, "daily", daily_floor)
            return
        if self.opts.liquidation.enabled and self.positions:
            mm = self.opts.liquidation.maintenance_margin_rate * self.gross_notional()
            if eq <= mm + EPS:                                 # cross margin  # ASSUMPTION
                self._fail(ts, "liquidation", eq)
                return
        self._check_target(ts, eq)

    def _check_target(self, ts: int, eq: float) -> None:
        tb = self.target_balance
        if tb is None:
            return
        metric = self.balance if self.opts.target_metric == "balance" else eq
        if metric >= tb - EPS:
            self.target_reached = True
            if self.trading_days_count >= self.min_trading_days:
                self._flatten_at_marks(ts, reason="close_on_pass")
                self.status = "passed"
                self.passed_ts = ts
                self.events.append(Event(ts, "pass", {"balance": self.balance, "days": self.trading_days_count}))

    def _flatten_at_floor(self, ts: int, floor: float, reason: str) -> None:
        """Закрыть всё по цене, соответствующей floor (не лучше), с taker-комиссией."""
        fees = sum(self._fee(p.notional_mark, "taker") for p in self.positions.values())
        for pid in list(self.positions):
            p = self.positions.pop(pid)
            self.events.append(Event(ts, reason, {"id": p.id, "symbol": p.symbol, "qty": p.qty}))
        self.balance = floor - fees

    def _flatten_at_marks(self, ts: int, reason: str) -> None:
        for pid in list(self.positions):
            p = self.positions[pid]
            self._close_position(p, p.mark, ts, liquidity="taker", reason=reason)

    def _fail(self, ts: int, reason: str, floor: float) -> None:
        self._flatten_at_floor(ts, floor, "force_close")
        self.status = "failed"
        self.fail_reason = reason
        self.failed_ts = ts
        self.events.append(Event(ts, "fail", {"reason": reason, "balance": self.balance}))

    def _protect(self, ts: int, kind: str, floor: float) -> None:
        self._flatten_at_floor(ts, floor, "force_close")
        self.day_start_balance = self.balance            # лимит обновлён  # ASSUMPTION
        self._old_floor, self._old_floor_until = None, None
        self.events.append(Event(ts, kind, {"balance": self.balance}))
