"""Risk governor для live-плана сделки (раздел 5). Формулы те же, что в numba-симуляторе
(quant.backtest.sim.simulate, блок «входы»), — перекрёстная проверка в tests/backtest/test_governor.py.

risk_usd = min(risk_per_trade × min(дневной запас, общий запас), abs_cap × size)  (или фиксированный %),
qty = risk_usd / (|entry − SL| + entry × (fee_in + taker) + ожидаемое проскальзывание),
notional ограничен max_per_lot, остатком max_total, плечом (5 × equity − gross), коррелированным риском.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from quant.backtest.challenge import RiskParams
from quant.instruments import Instrument
from quant.propsim import Account


@dataclass
class Plan:
    notional_usd: float | None
    qty: float | None
    risk_usd: float | None
    risk_pct_daily_room: float | None
    risk_pct_total_room: float | None
    blocked_reason: str | None

    def as_dict(self) -> dict[str, Any]:
        return self.__dict__.copy()


def plan_trade(acc: Account, inst: Instrument, *, side: int, entry: float, sl: float, risk: RiskParams,
               entry_liquidity: str = "taker", expected_slip: float = 0.0, alt_open_risk_usd: float = 0.0,
               month_room_usd: float | None = None) -> Plan:
    """month_room_usd — свободный месячный лимит по правилу 6% Элдера (quant.live.journal.month_room);
    None — правило не применяется (как в симуляторе)."""

    def blocked(reason: str) -> Plan:
        return Plan(None, None, None, None, None, reason)

    if acc.status != "active":
        return blocked(f"счёт в статусе {acc.status}")
    if risk.target_lock and acc.target_reached and acc.trading_days_count < acc.min_trading_days:
        return blocked("цель достигнута — режим добора дней (target_lock)")
    same = [p for p in acc.positions.values() if p.symbol == inst.hh_symbol]
    if any(p.side != side for p in same):
        return blocked("встречная позиция по инструменту")
    if len(acc.positions) >= risk.max_concurrent_positions:
        return blocked("достигнут лимит одновременных позиций")
    eq = acc.equity()
    if risk.soft_daily_stop > 0 and (acc.day_start_balance - eq) >= risk.soft_daily_stop * acc.daily_limit_usd:
        return blocked(f"soft daily stop: дневная потеря ≥ {risk.soft_daily_stop:.0%} лимита")
    d_room, t_room = acc.daily_room(), acc.total_room()
    room = min(d_room, t_room)
    if room <= 0:
        return blocked("нет запаса до лимита")
    if month_room_usd is not None and month_room_usd <= 0:
        return blocked("правило 6% Элдера: месячный лимит убытка и риска исчерпан — до конца месяца без новых сделок")
    if risk.fixed_risk_pct > 0:
        risk_usd = risk.fixed_risk_pct * acc.size
    else:
        risk_usd = min(risk.risk_per_trade * room, risk.abs_risk_cap_pct * acc.size)
    if month_room_usd is not None:
        risk_usd = min(risk_usd, month_room_usd)
    fees = acc.market.fees
    fee_in = fees.maker if entry_liquidity == "maker" else fees.taker
    per_unit = abs(entry - sl) + entry * (fee_in + fees.taker) + expected_slip
    if per_unit <= 0:
        return blocked("некорректный SL")
    notional = risk_usd / per_unit * entry
    notional = min(notional, inst.max_per_lot_usd)
    notional = min(notional, inst.max_total_usd - sum(p.notional_mark for p in same))
    base = eq if acc.market.leverage_base == "equity" else acc.balance
    notional = min(notional, acc.market.max_leverage * base - acc.gross_notional())
    if inst.group == "crypto_alt" and risk.max_correlated_exposure_pct > 0:
        allowed = risk.max_correlated_exposure_pct * acc.size - alt_open_risk_usd
        if allowed <= 0:
            return blocked("исчерпан лимит коррелированного риска по альтам")
        notional = min(notional, allowed / abs(entry - sl) * entry)
    if notional < risk.min_notional_usd or notional <= 0:
        return blocked("размер меньше минимального (лимиты/плечо)")
    qty = notional / entry
    real_risk = qty * per_unit
    return Plan(notional_usd=notional, qty=qty, risk_usd=real_risk,
                risk_pct_daily_room=real_risk / d_room if d_room > 0 else None,
                risk_pct_total_room=real_risk / t_room if t_room > 0 else None, blocked_reason=None)
