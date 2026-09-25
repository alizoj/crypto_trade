"""Остальные обязательные тесты propsim (2.6): общий лимит, DLP, торговые дни, отказы ордеров,
окно сброса, equity по mark price, funding от mark, одностадийный челлендж, рынок US Stocks."""

from __future__ import annotations

import pytest

from quant.propsim import Account, OrderRejected
from quant.timeutil import to_min, utc

D1 = to_min(utc(2026, 3, 2, 21, 0))     # внутри проп-дня (день начался 2026-03-02 20:00 UTC)
MIN = 1
DAY = 1440


def acc_new(cfg, **kw):
    params = dict(challenge_type="two_stage", stage="stage1", size=10_000, start_ts=D1)
    params.update(kw)
    return Account.new(cfg, **params)


def lose_day(acc: Account, ts: int, amount: float, symbol: str = "BTCUSDT") -> None:
    """Открыть и закрыть позицию с убытком amount (без комиссий)."""
    p = acc.open(symbol, "long", qty=1, price=10_000, ts=ts, fee=0.0)
    acc.close(p.id, price=10_000 - amount, ts=ts + 5, fee=0.0)


# ------------------------------------------------------------------ общий лимит

def test_total_limit_static(cfg):
    acc = acc_new(cfg)
    assert acc.total_floor == pytest.approx(9_000)
    lose_day(acc, D1, 450)
    acc.advance(D1 + DAY)
    lose_day(acc, D1 + DAY, 450)
    acc.advance(D1 + 2 * DAY)
    assert acc.balance == pytest.approx(9_100)
    assert acc.daily_floor == pytest.approx(8_600)      # дневной ниже общего
    p = acc.open("BTCUSDT", "long", qty=1, price=10_000, ts=D1 + 2 * DAY, fee=0.0)
    acc.update_marks({"BTCUSDT": 9_899}, D1 + 2 * DAY + 1)   # equity 8 999 < 9 000
    assert acc.status == "failed" and acc.fail_reason == "total"
    assert acc.equity() <= 9_000 + 1e-9
    assert p.id not in acc.positions


def test_total_limit_trailing(cfg):
    c = cfg.with_options(total_floor_mode="trailing", trailing_reference="balance")
    acc = acc_new(c)
    p = acc.open("BTCUSDT", "long", qty=1, price=10_000, ts=D1, fee=0.0)
    acc.close(p.id, price=10_400, ts=D1 + 1, fee=0.0)    # баланс 10 400 → floor 9 400
    assert acc.total_floor == pytest.approx(9_400)


def test_total_breach_not_covered_by_protection(cfg):
    c = cfg.with_options(daily_loss_protection=True)
    acc = acc_new(c, balance=9_300)                       # общий floor 9 000, дневной 8 800
    acc.open("BTCUSDT", "long", qty=1, price=10_000, ts=D1, fee=0.0)
    acc.update_marks({"BTCUSDT": 9_690}, D1 + 1)         # equity 8 990: общий пробит
    assert acc.status == "failed" and acc.fail_reason == "total"


# ------------------------------------------------------------------ Daily Loss Protection

def test_daily_loss_protection_once(cfg):
    c = cfg.with_options(daily_loss_protection=True)
    acc = acc_new(c, balance=10_700)                      # запас до общего floor 9 000, чтобы второй пробой был дневным
    acc.open("BTCUSDT", "long", qty=1, price=10_000, ts=D1, fee=0.0)
    acc.update_marks({"BTCUSDT": 9_490}, D1 + 1)         # −510 → пробой дневного (floor 10 200)
    assert acc.status == "active"
    assert acc.dlp_available is False
    assert not acc.positions
    assert acc.balance <= 10_200 + 1e-9                   # закрыто не лучше floor
    new_floor = acc.daily_floor
    assert new_floor == pytest.approx(acc.balance - 500)  # лимит обновлён
    assert any(e.kind == "daily_loss_protection" for e in acc.events)
    acc.open("BTCUSDT", "long", qty=1, price=10_000, ts=D1 + 2, fee=0.0)
    acc.update_marks({"BTCUSDT": 9_490}, D1 + 3)         # второй пробой → провал
    assert acc.status == "failed" and acc.fail_reason == "daily"


def test_daily_loss_reset_only_stages_1_2(cfg):
    c = cfg.with_options(daily_loss_reset=True)
    acc = acc_new(c, stage="trader")
    acc.open("BTCUSDT", "long", qty=1, price=10_000, ts=D1, fee=0.0)
    acc.update_marks({"BTCUSDT": 9_490}, D1 + 1)
    assert acc.status == "failed"                         # на стадии Трейдер Reset не работает
    acc2 = acc_new(c, stage="stage2")
    acc2.open("BTCUSDT", "long", qty=1, price=10_000, ts=D1, fee=0.0)
    acc2.update_marks({"BTCUSDT": 9_490}, D1 + 1)
    assert acc2.status == "active"


# ------------------------------------------------------------------ торговые дни

def test_trading_days_by_boundary(cfg):
    acc = acc_new(cfg, start_ts=to_min(utc(2026, 3, 2, 19, 0)))
    t_before = to_min(utc(2026, 3, 2, 19, 59))            # ещё день «2 марта UTC+4»
    t_after = to_min(utc(2026, 3, 2, 20, 11))             # новый проп-день (после окна)
    p = acc.open("BTCUSDT", "long", qty=0.1, price=10_000, ts=t_before, fee=0.0)
    acc.advance(t_after)
    acc.close(p.id, price=10_000, ts=t_after, fee=0.0)    # закрытие в другой день — не новый торговый день
    assert acc.trading_days_count == 1
    acc.open("BTCUSDT", "long", qty=0.1, price=10_000, ts=t_after + 1, fee=0.0)
    assert acc.trading_days_count == 2


def test_pass_requires_min_trading_days(cfg):
    acc = acc_new(cfg)
    p = acc.open("BTCUSDT", "long", qty=1, price=10_000, ts=D1, fee=0.0)
    acc.close(p.id, price=10_900, ts=D1 + 1, fee=0.0)     # +9% за один день
    assert acc.target_reached and acc.status == "active"  # дней мало
    for d in range(1, 5):
        t = D1 + d * DAY
        acc.advance(t)
        q = acc.open("BTCUSDT", "long", qty=0.01, price=10_000, ts=t, fee=0.0)
        if acc.status == "passed":          # цель уже есть, 5-й торговый день набран открытием
            break
        acc.close(q.id, price=10_000, ts=t + 1, fee=0.0)
    assert acc.status == "passed"
    assert acc.trading_days_count == 5


def test_target_metric_equity_option(cfg):
    c = cfg.with_options(target_metric="equity")
    acc = acc_new(c)
    for d in range(5):
        t = D1 + d * DAY
        acc.advance(t)
        q = acc.open("BTCUSDT", "long", qty=0.01, price=10_000, ts=t, fee=0.0)
        acc.close(q.id, price=10_000, ts=t + 1, fee=0.0)
    acc.open("BTCUSDT", "long", qty=1, price=10_000, ts=D1 + 4 * DAY + 2, fee=0.0)
    acc.update_marks({"BTCUSDT": 10_800}, D1 + 4 * DAY + 3)
    assert acc.status == "passed"


# ------------------------------------------------------------------ отказы ордеров

def test_reject_max_per_lot(cfg):
    acc = acc_new(cfg, size=200_000)
    with pytest.raises(OrderRejected, match="max_per_lot"):
        acc.open("FILUSDT", "long", qty=2_000, price=3.0, ts=D1)      # 6 000 > 5 000


def test_reject_max_total(cfg):
    acc = acc_new(cfg, size=200_000)
    acc.open("TRXUSDT", "long", qty=100_000, price=0.4, ts=D1)          # 40 000
    acc.open("TRXUSDT", "long", qty=100_000, price=0.4, ts=D1)          # 80 000
    with pytest.raises(OrderRejected, match="max_total"):
        acc.open("TRXUSDT", "long", qty=100_000, price=0.4, ts=D1)      # 120 000 > 100 000


def test_reject_leverage(cfg):
    acc = acc_new(cfg)
    acc.open("BTCUSDT", "long", qty=4, price=10_000, ts=D1)             # 40 000
    with pytest.raises(OrderRejected, match="leverage"):
        acc.open("ETHUSDT", "long", qty=6, price=2_000, ts=D1)          # итого 52 000 > 5 × 9 988


def test_reject_opposite_side_same_symbol(cfg):
    acc = acc_new(cfg)
    acc.open("BTCUSDT", "long", qty=0.1, price=10_000, ts=D1)
    with pytest.raises(OrderRejected, match="hedge"):
        acc.open("BTCUSDT", "short", qty=0.1, price=10_000, ts=D1)


# ------------------------------------------------------------------ окно сброса 20:00–20:10 UTC

@pytest.mark.parametrize("mode, expect_fail", [("new_day", False), ("old_day", True), ("both", True)])
def test_reset_window_funding_counts(cfg, mode, expect_fail):
    """Funding −60 в 20:00 UTC при equity чуть выше floor старого дня.

    Старый день: старт 10 000 (floor 9 500), реализовано −300 → баланс 9 700, открытая позиция −150
    → equity 9 550. Новый день фиксирует баланс 9 700 → floor 9 200.
    new_day: списание относится к новому дню → 9 490 > 9 200, не провал;
    old_day/both: списание проверяется и против floor старого дня 9 500 → провал.
    """
    c = cfg.with_options(reset_window_mode=mode)
    start = to_min(utc(2026, 3, 2, 21, 0))
    acc = Account.new(c, challenge_type="two_stage", stage="stage1", size=10_000, start_ts=start)
    lose_day(acc, start, 300)
    acc.open("BTCUSDT", "long", qty=1, price=10_000, ts=start + 30, fee=0.0)
    acc.update_marks({"BTCUSDT": 9_850}, start + 60)                  # −150: equity 9 550, floor 9 500
    assert acc.status == "active"
    t_reset = to_min(utc(2026, 3, 3, 20, 0))
    acc.advance(t_reset)
    acc.funding("BTCUSDT", rate=60 / 9_850, mark_price=9_850, ts=t_reset)   # −60 → equity 9 490
    assert (acc.status == "failed") is expect_fail


def test_reset_window_old_day_snapshot_after_window(cfg):
    c = cfg.with_options(reset_window_mode="old_day")
    start = to_min(utc(2026, 3, 2, 21, 0))
    acc = Account.new(c, challenge_type="two_stage", stage="stage1", size=10_000, start_ts=start)
    p = acc.open("BTCUSDT", "long", qty=1, price=10_000, ts=start, fee=0.0)
    t_reset = to_min(utc(2026, 3, 3, 20, 0))
    acc.advance(t_reset + 5)
    acc.close(p.id, price=10_100, ts=t_reset + 5, fee=0.0)            # закрыто в окне
    assert acc.day_start_balance == pytest.approx(10_000)            # фиксация ещё не произошла
    acc.advance(t_reset + 10)
    assert acc.day_start_balance == pytest.approx(10_100)            # фиксация после окна


# ------------------------------------------------------------------ equity по mark, funding от mark

def test_intrabar_wick_on_last_does_not_fail_with_mark(cfg):
    acc = acc_new(cfg)
    acc.open("BTCUSDT", "long", qty=5, price=10_000, ts=D1, fee=0.0)
    # last-цена фитилём ушла к 9 890 (−550), mark — только к 9 950 (−250)
    acc.check_intrabar(D1 + 1, worst_mark={"BTCUSDT": (9_950, 10_010)}, worst_last={"BTCUSDT": (9_890, 10_010)})
    assert acc.status == "active"


def test_intrabar_wick_on_last_fails_in_last_mode(cfg):
    c = cfg.with_options(equity_price_source="last")
    acc = acc_new(c)
    acc.open("BTCUSDT", "long", qty=5, price=10_000, ts=D1, fee=0.0)
    acc.check_intrabar(D1 + 1, worst_mark={"BTCUSDT": (9_950, 10_010)}, worst_last={"BTCUSDT": (9_890, 10_010)})
    assert acc.status == "failed" and acc.fail_reason == "daily"


def test_intrabar_uses_high_for_shorts(cfg):
    acc = acc_new(cfg)
    acc.open("BTCUSDT", "short", qty=5, price=10_000, ts=D1, fee=0.0)
    acc.check_intrabar(D1 + 1, worst_mark={"BTCUSDT": (9_900, 10_101)}, worst_last={"BTCUSDT": (9_900, 10_101)})
    assert acc.status == "failed"


def test_funding_uses_mark_price_not_entry(cfg):
    acc = acc_new(cfg)
    acc.open("BTCUSDT", "long", qty=2, price=10_000, ts=D1, fee=0.0)
    acc.funding("BTCUSDT", rate=0.001, mark_price=12_000, ts=D1 + 1)
    assert acc.balance == pytest.approx(10_000 - 2 * 12_000 * 0.001)
    acc.funding("BTCUSDT", rate=-0.001, mark_price=12_000, ts=D1 + 2)   # rate < 0 — платит шорт, лонг получает
    assert acc.balance == pytest.approx(10_000)


def test_short_receives_positive_funding(cfg):
    acc = acc_new(cfg)
    acc.open("BTCUSDT", "short", qty=1, price=10_000, ts=D1, fee=0.0)
    acc.funding("BTCUSDT", rate=0.0005, mark_price=10_000, ts=D1 + 1)
    assert acc.balance == pytest.approx(10_005)


# ------------------------------------------------------------------ одностадийный челлендж

def test_one_stage_config(cfg):
    acc = Account.new(cfg, challenge_type="one_stage", stage="stage1", size=10_000, start_ts=D1)
    assert acc.daily_limit_usd == pytest.approx(300)
    assert acc.total_floor == pytest.approx(9_400)
    assert acc.target_balance == pytest.approx(11_000)
    assert acc.min_trading_days == 3


def test_stage_start_balance_is_account_size(cfg):
    acc = acc_new(cfg, stage="stage2", size=25_000)
    assert acc.balance == pytest.approx(25_000)
    assert acc.target_balance == pytest.approx(26_500)
    assert acc.total_floor == pytest.approx(23_000)


# ------------------------------------------------------------------ комиссии

def test_fees_on_full_notional_open_and_close(cfg):
    acc = acc_new(cfg)
    p = acc.open("BTCUSDT", "long", qty=5, price=10_000, ts=D1, liquidity="taker")   # 15
    acc.close(p.id, price=10_000, ts=D1 + 1, liquidity="taker")                      # 15
    assert acc.balance == pytest.approx(10_000 - 30)


# ------------------------------------------------------------------ рынок US Stocks (фаза 8)

def test_us_stocks_rules(cfg):
    start = to_min(utc(2026, 3, 3, 15, 0))
    acc = Account.new(cfg, challenge_type="two_stage", stage="stage1", size=10_000, start_ts=start,
                      market="us_stocks")
    p = acc.open("AAPL", "long", qty=10, price=200, ts=start, liquidity="taker")      # 2 000 × 0.04% = 0.8 → min $1
    assert acc.balance == pytest.approx(10_000 - 1.0)
    with pytest.raises(OrderRejected, match="shares"):
        acc.open("AAPL", "long", qty=10_000, price=1, ts=start)
    with pytest.raises(OrderRejected, match="buying_power"):
        acc.open("MSFT", "long", qty=200, price=250, ts=start)                        # 2 000 + 50 000 > 5 × balance
    acc.update_marks({"AAPL": 201}, start + 60)
    with pytest.raises(OrderRejected, match="blocked"):
        acc.open("MSFT", "long", qty=1, price=250, ts=to_min(utc(2026, 3, 3, 19, 56)))
    assert p.id in acc.positions
    acc.advance(to_min(utc(2026, 3, 3, 19, 59)))                                      # автозакрытие
    assert p.id not in acc.positions
    assert acc.balance == pytest.approx(10_000 - 1.0 + 10 - 1.0)


def test_us_stocks_day_boundary_0400(cfg):
    start = to_min(utc(2026, 3, 3, 15, 0))
    acc = Account.new(cfg, challenge_type="two_stage", stage="stage1", size=10_000, start_ts=start,
                      market="us_stocks")
    d0 = acc.day_id
    acc.advance(to_min(utc(2026, 3, 4, 3, 59)))
    assert acc.day_id == d0
    acc.advance(to_min(utc(2026, 3, 4, 4, 0)))
    assert acc.day_id == d0 + 1
