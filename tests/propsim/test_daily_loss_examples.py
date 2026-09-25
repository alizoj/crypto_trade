"""Обязательные тест-кейсы 2.6: примеры из документации HashHedge (Boost, $10 000, лимит $500).

Каждый шаг проверяет balance / unrealized / equity / daily_loss.
"""

from __future__ import annotations

import pytest

from quant.propsim import Account
from quant.timeutil import to_min, utc

# День открывается в 20:00 UTC (00:00 UTC+4). Берём момент внутри дня, после окна сброса.
DAY0 = to_min(utc(2026, 3, 2, 21, 0))
NEXT_DAY = to_min(utc(2026, 3, 3, 20, 30))


def new_boost(cfg, balance: float = 10_000.0, ts: int = DAY0) -> Account:
    return Account.new(cfg, challenge_type="two_stage", stage="stage1", size=10_000, start_ts=ts, balance=balance)


def state(acc: Account) -> tuple[float, float, float, float]:
    return (round(acc.balance, 6), round(acc.unrealized(), 6), round(acc.equity(), 6), round(acc.daily_loss(), 6))


def test_1_normal_day(cfg):
    acc = new_boost(cfg)
    assert acc.daily_floor == pytest.approx(9_500)
    # Открытие: notional 50 000 maker 0.01% → fee 5
    pos = acc.open("BTCUSDT", "long", qty=5, price=10_000, ts=DAY0 + 1, liquidity="maker")
    assert state(acc) == (9_995, 0, 9_995, -5)
    acc.update_marks({"BTCUSDT": 9_920}, DAY0 + 2)
    assert state(acc) == (9_995, -400, 9_595, -405)
    # Funding −50: лонг платит при rate > 0, от mark price
    acc.funding("BTCUSDT", rate=50 / (5 * 9_920), mark_price=9_920, ts=DAY0 + 3)
    assert state(acc) == pytest.approx((9_945, -400, 9_545, -455))
    assert acc.status == "active"
    acc.update_marks({"BTCUSDT": 9_911}, DAY0 + 4)   # позиция −445
    assert acc.status == "failed"
    assert acc.fail_reason == "daily"
    assert pos.id not in acc.positions


def test_2_balance_above_start(cfg):
    acc = new_boost(cfg, balance=10_700)
    assert acc.daily_floor == pytest.approx(10_200)   # лимит всё равно 500, а не 535
    acc.open("BTCUSDT", "long", qty=5, price=10_000, ts=DAY0 + 1, liquidity="maker")
    assert state(acc) == (10_695, 0, 10_695, -5)
    acc.update_marks({"BTCUSDT": 9_920}, DAY0 + 2)
    assert state(acc) == (10_695, -400, 10_295, -405)
    acc.funding("BTCUSDT", rate=50 / (5 * 9_920), mark_price=9_920, ts=DAY0 + 3)
    assert state(acc) == pytest.approx((10_645, -400, 10_245, -455))
    acc.update_marks({"BTCUSDT": 9_911}, DAY0 + 4)
    assert acc.status == "failed" and acc.fail_reason == "daily"


def test_3_two_positions_netted(cfg):
    acc = new_boost(cfg)
    acc.open("ETHUSDT", "long", qty=10, price=2_000, ts=DAY0 + 1, liquidity="taker")   # fee 6
    acc.open("SOLUSDT", "long", qty=200, price=100, ts=DAY0 + 1, liquidity="maker")    # fee 2
    assert state(acc) == (9_992, 0, 9_992, -8)
    acc.update_marks({"ETHUSDT": 1_960, "SOLUSDT": 101}, DAY0 + 2)                     # A −400, B +200
    assert state(acc) == (9_992, -200, 9_792, -208)
    assert acc.status == "active"


def test_4_losing_position_carried(cfg):
    acc = new_boost(cfg)
    acc.open("BTCUSDT", "long", qty=10, price=1_000, ts=DAY0 + 1, fee=0.0)
    acc.update_marks({"BTCUSDT": 970}, DAY0 + 2)                                       # −300
    acc.advance(NEXT_DAY)                                                              # новый день
    assert acc.day_start_balance == pytest.approx(10_000)
    assert state(acc) == (10_000, -300, 9_700, -300)
    acc.funding("BTCUSDT", rate=50 / (10 * 970), mark_price=970, ts=NEXT_DAY + 1)
    assert state(acc) == pytest.approx((9_950, -300, 9_650, -350))
    acc.open("ETHUSDT", "long", qty=5, price=2_000, ts=NEXT_DAY + 2, liquidity="taker")  # fee 3
    assert state(acc) == pytest.approx((9_947, -300, 9_647, -353))
    assert acc.status == "active"
    acc.update_marks({"ETHUSDT": 2_000 - 147 / 5}, NEXT_DAY + 3)                        # 2-я позиция −147
    assert acc.status == "failed" and acc.fail_reason == "daily"


def test_5_winning_position_carried(cfg):
    acc = new_boost(cfg)
    acc.open("BTCUSDT", "long", qty=10, price=1_000, ts=DAY0 + 1, fee=0.0)
    acc.update_marks({"BTCUSDT": 1_030}, DAY0 + 2)                                     # +300
    acc.advance(NEXT_DAY)
    assert state(acc) == (10_000, 300, 10_300, 300)
    assert acc.daily_floor == pytest.approx(9_500)
    acc.update_marks({"BTCUSDT": 1_000}, NEXT_DAY + 1)
    assert state(acc) == (10_000, 0, 10_000, 0)
    acc.update_marks({"BTCUSDT": 970}, NEXT_DAY + 2)
    assert state(acc) == (10_000, -300, 9_700, -300)
    acc.funding("BTCUSDT", rate=50 / (10 * 970), mark_price=970, ts=NEXT_DAY + 3)
    assert state(acc) == pytest.approx((9_950, -300, 9_650, -350))
    acc.update_marks({"BTCUSDT": 955}, NEXT_DAY + 4)                                   # −450
    assert acc.status == "failed" and acc.fail_reason == "daily"
