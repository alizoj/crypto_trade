"""Модель исполнения (раздел 5): вход, SL/TP, неоднозначный бар, гэп, таймаут, limit, halted, funding."""

from __future__ import annotations

from datetime import timedelta

import numpy as np
import polars as pl
import pytest

from quant.research.barriers import (EXIT_SL, EXIT_TIMEOUT, EXIT_TP1, EXIT_TP2, FILLED, INVALID, NO_DATA,
                                     NO_FILL, ExecParams, execute_signals)
from tests.backtest.helpers import T0, bars_from_ohlc, random_walk_bars, signals_df

P0 = ExecParams(exec_delay_min=(0, 0), slippage_k_atr=0.0)


def run(bars, sigs, params=P0, **kw):
    return execute_signals(bars, sigs, params, hh_symbol="BTCUSDT", **kw)


def flat(n, p=100.0):
    return [(p, p + 0.1, p - 0.1, p)] * n


def test_market_long_tp_hit():
    rows = flat(3) + [(100, 101.5, 99.9, 101)] + flat(2, 101)
    bars = bars_from_ohlc(rows)
    sig = signals_df([{"ts": T0 + timedelta(minutes=2), "side": 1, "entry_price": 100, "sl": 99, "tp1": 101}])
    t = run(bars, sig).trades.row(0, named=True)
    assert t["status"] == FILLED and t["entry_min"] * 60_000 == int((T0 + timedelta(minutes=2)).timestamp() * 1000)
    assert t["x1_kind"] == EXIT_TP1 and t["label"] == 1
    assert t["x1_px"] == pytest.approx(101.0)
    assert t["r_gross"] == pytest.approx(1.0)


def test_sl_first_when_both_in_bar():
    rows = flat(2) + [(100, 101.5, 98.5, 100)] + flat(2)
    bars = bars_from_ohlc(rows)
    sig = signals_df([{"ts": T0 + timedelta(minutes=1), "side": 1, "entry_price": 100, "sl": 99, "tp1": 101}])
    t = run(bars, sig).trades.row(0, named=True)
    assert t["x1_kind"] == EXIT_SL and t["label"] == 0 and t["ambiguous"]
    assert t["r_gross"] == pytest.approx(-1.0)


def test_gap_through_stop_fills_at_open():
    rows = flat(2) + [(97, 97.5, 96.5, 97)] + flat(2, 97)
    bars = bars_from_ohlc(rows)
    sig = signals_df([{"ts": T0 + timedelta(minutes=1), "side": 1, "entry_price": 100, "sl": 99, "tp1": 101}])
    t = run(bars, sig).trades.row(0, named=True)
    assert t["x1_kind"] == EXIT_SL and t["x1_px"] == pytest.approx(97.0)
    assert t["r_gross"] == pytest.approx(-3.0)


def test_short_and_timeout():
    bars = bars_from_ohlc(flat(200))
    sig = signals_df([{"ts": T0, "side": -1, "entry_price": 100, "sl": 102, "tp1": 98, "max_hold_min": 60}])
    t = run(bars, sig).trades.row(0, named=True)
    assert t["x1_kind"] == EXIT_TIMEOUT and t["label"] == 2 and t["hold_min"] == 60


def test_partial_tp_then_tp2():
    rows = flat(1) + [(100, 101.2, 99.9, 101)] + flat(3, 101) + [(101, 102.2, 100.9, 102)] + flat(2, 102)
    bars = bars_from_ohlc(rows)
    sig = signals_df([{"ts": T0, "side": 1, "entry_price": 100, "sl": 99, "tp1": 101, "tp2": 102, "tp1_frac": 0.5}])
    t = run(bars, sig).trades.row(0, named=True)
    assert t["x1_kind"] == EXIT_TP1 and t["x1_frac"] == pytest.approx(0.5)
    assert t["x2_kind"] == EXIT_TP2 and t["x2_frac"] == pytest.approx(0.5)
    assert t["r_gross"] == pytest.approx(1.5)


def test_limit_fill_requires_pass_through():
    # low касается 99.5 ровно — не исполняется; следующий бар проходит сквозь
    rows = flat(1) + [(100, 100.2, 99.5, 100)] + [(100, 100.1, 99.4, 99.6)] + flat(3, 99.6)
    bars = bars_from_ohlc(rows)
    sig = signals_df([{"ts": T0, "side": 1, "entry_type": "limit", "entry_price": 99.5, "sl": 98.5, "tp1": 101}])
    t = run(bars, sig).trades.row(0, named=True)
    assert t["status"] == FILLED and t["entry_liq"] == 0 and t["entry_px"] == pytest.approx(99.5)
    assert t["entry_min"] * 60_000 == int((T0 + timedelta(minutes=2)).timestamp() * 1000)


def test_limit_expires():
    bars = bars_from_ohlc(flat(200))
    sig = signals_df([{"ts": T0, "side": 1, "entry_type": "limit", "entry_price": 95, "sl": 94, "tp1": 101,
                       "valid_min": 30}])
    assert run(bars, sig).trades["status"][0] == NO_FILL


def test_invalid_if_price_beyond_sl_at_entry():
    rows = flat(1) + [(98.5, 98.6, 98.4, 98.5)] + flat(3, 98.5)
    bars = bars_from_ohlc(rows)
    sig = signals_df([{"ts": T0 + timedelta(minutes=1), "side": 1, "entry_price": 100, "sl": 99, "tp1": 101}])
    assert run(bars, sig).trades["status"][0] == INVALID


def test_halted_bars_no_fill():
    bars = bars_from_ohlc(flat(10), halted=[True] * 10)
    sig = signals_df([{"ts": T0, "side": 1, "entry_price": 100, "sl": 99, "tp1": 101}])
    assert run(bars, sig).trades["status"][0] == NO_DATA


def test_mark_trigger_and_path():
    # last-фитиль до 98.9 пробивает SL 99, mark только до 99.5
    rows = flat(1) + [(100, 100.2, 98.9, 100)] + flat(3)
    mark = flat(1) + [(100, 100.2, 99.5, 100)] + flat(3)
    bars = bars_from_ohlc(rows, mark=mark)
    sig = signals_df([{"ts": T0, "side": 1, "entry_price": 100, "sl": 99, "tp1": 101, "max_hold_min": 4}])
    t_last = run(bars, sig).trades.row(0, named=True)
    assert t_last["x1_kind"] == EXIT_SL
    res = run(bars, sig, ExecParams(exec_delay_min=(0, 0), slippage_k_atr=0.0, sl_tp_trigger="mark"))
    t_mark = res.trades.row(0, named=True)
    assert t_mark["x1_kind"] == EXIT_TIMEOUT
    # путь: худшая mark-цена второго бара = 99.5, last = 98.9
    s, n = t_mark["p_start"], t_mark["p_len"]
    assert n == 5
    assert res.paths["p_wm_lo"][s + 1] == pytest.approx(99.5)
    assert res.paths["p_wl_lo"][s + 1] == pytest.approx(98.9)


def test_slippage_and_delay():
    bars = bars_from_ohlc([(100 + i * 0.01, 100.2 + i * 0.01, 99.8 + i * 0.01, 100 + i * 0.01) for i in range(100)])
    sig = signals_df([{"ts": T0, "side": 1, "entry_price": 100, "sl": 98, "tp1": 110, "max_hold_min": 30}])
    p = ExecParams(exec_delay_min=(5, 5), slippage_k_atr=0.1)
    t = execute_signals(bars, sig, p, hh_symbol="X").trades.row(0, named=True)
    assert t["delay_min"] == 5
    assert t["entry_px"] > 100.05                       # open 5-й минуты + проскальзывание
    assert t["entry_slip"] > 0


def test_funding_attached_to_trade_by_mark():
    bars = bars_from_ohlc(flat(600))
    fund = pl.DataFrame({
        "ts": pl.Series([int((T0 + timedelta(minutes=m)).timestamp() * 1000) for m in (100, 480, 590)]).cast(pl.Datetime("ms", "UTC")),
        "rate": [0.001, 0.001, 0.001], "mark_price": [100.0, 110.0, None], "source": ["binance_um"] * 3,
    })
    sig = signals_df([{"ts": T0 + timedelta(minutes=50), "side": 1, "entry_price": 100, "sl": 90, "tp1": 120,
                       "max_hold_min": 500}])
    res = run(bars, sig, funding=fund)
    t = res.trades.row(0, named=True)
    assert t["f_len"] == 2                               # 100 и 480 внутри удержания, 590 — после выхода
    amts = res.funding["f_amt"][t["f_start"]:t["f_start"] + t["f_len"]]
    assert amts == pytest.approx([-0.1, -0.11])          # лонг платит, от mark на момент начисления


def test_random_walk_symmetric_barriers_is_fair():
    """Мартингал + симметричные барьеры → доля TP ≈ 0.5 (ступень A не смещена)."""
    bars = random_walk_bars(200_000, seed=3, sigma=0.0005)
    closes = bars["close"].to_numpy()
    idx = np.arange(100, 199_000, 40)
    rows = []
    for i in idx:
        p = float(closes[i - 1])
        rows.append({"ts": T0 + timedelta(minutes=int(i)), "side": 1 if i % 80 == 20 else -1,
                     "entry_price": p, "sl": p * (1 - 0.004) if i % 80 == 20 else p * 1.004,
                     "tp1": p * 1.004 if i % 80 == 20 else p * (1 - 0.004), "max_hold_min": 100_000})
    sig = signals_df(rows)
    t = run(bars, sig, record_path=False).trades.filter(pl.col("status") == FILLED)
    lab = t.filter(pl.col("label") >= 0)["label"].to_numpy()
    p_tp = float(np.mean(lab == 1))
    se = np.sqrt(0.25 / len(lab))
    assert abs(p_tp - 0.5) < 3.5 * se + 0.01
