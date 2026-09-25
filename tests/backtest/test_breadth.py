"""Широта рынка: расчёт доли, фильтр в feature_rules, передача ряда движком."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import polars as pl
import pytest

from quant import instruments as I
from quant.backtest import breadth as B
from quant.data.aggregate import resample
from quant.indicators.synthetic import random_walk_bars
from quant.strategies import get_strategy

START = datetime(2024, 4, 1, tzinfo=timezone.utc)
END = datetime(2024, 6, 1, tzinfo=timezone.utc)
RULE_L = [["rsi14", "<=", 40]]
RULE_S = [["rsi14", ">=", 60]]


@pytest.fixture(scope="module")
def market():
    syms = ["ETHUSDT", "SOLUSDT", "XRPUSDT", "DOGEUSDT"]
    bars = {s: random_walk_bars(120 * 1440, tf="1m", start="2024-02-01", seed=11 + k, sigma_1m=0.0008,
                                regime_bars=3000) for k, s in enumerate(syms)}
    return {s: I.get(s) for s in syms}, bars


@pytest.fixture
def no_ext(monkeypatch):
    """Внешние ряды (funding/metrics/bookDepth) — пустые: тест не зависит от данных на диске."""
    from quant.data import store

    monkeypatch.setattr(store, "load_funding", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(store, "load_metrics", lambda *a, **k: pl.DataFrame())
    monkeypatch.setattr(store, "load_book_depth", lambda *a, **k: pl.DataFrame())
    B._CACHE.clear()


def test_breadth_fraction_matches_per_symbol_rules(market, no_ext):
    insts, bars = market
    b = B.breadth_series(list(insts.values()), START, END, tf="1h", htf_tf="4h", rules_long=RULE_L, rules_short=RULE_S,
                         load_bars=lambda inst: bars[inst.hh_symbol], warmup_days=60)
    assert b["close_ts"].is_sorted() and b.height > 1000
    assert (b["n_sym"] == 4).all()                                    # все символы готовы на всём окне
    # ручной пересчёт на одном часе: доля символов с RSI <= 40 на закрытии этого бара
    t = b.filter(pl.col("n_long") > 0)["close_ts"][5]
    hits = 0
    for s in insts:
        fr = get_strategy("feature_rules", {"rules_long": RULE_L}).compute(resample(bars[s], "1h"), hh_symbol=s,
                                                                          tf="1h", bars_1m=bars[s])
        hits += int(fr.filter(pl.col("close_ts") == t)["rsi14"][0] <= 40)
    assert b.filter(pl.col("close_ts") == t)["frac_long"][0] == pytest.approx(hits / 4)
    assert ((b["frac_long"] >= 0) & (b["frac_long"] <= 1)).all()


def test_strategy_gate(market, no_ext):
    insts, bars = market
    b = B.breadth_series(list(insts.values()), START, END, tf="1h", htf_tf="4h", rules_long=RULE_L, rules_short=RULE_S,
                         load_bars=lambda inst: bars[inst.hh_symbol], warmup_days=60)
    s = "SOLUSDT"
    base = {"rules_long": RULE_L, "rules_short": RULE_S, "rearm_bars": 6}
    free = get_strategy("feature_rules", base).compute(resample(bars[s], "1h"), hh_symbol=s, tf="1h", bars_1m=bars[s],
                                                       meta={"breadth": b})
    gated = get_strategy("feature_rules", {**base, "breadth_min_frac": 0.75}).compute(
        resample(bars[s], "1h"), hh_symbol=s, tf="1h", bars_1m=bars[s], meta={"breadth": b})
    g = gated.filter(pl.col("signal_long"))
    assert g.height > 0 and (g["breadth_long"] >= 0.75).all()
    # фильтр только отсекает сигналы после правила «один вход на эпизод», новых не добавляет
    assert set(g["close_ts"].to_list()) <= set(free.filter(pl.col("signal_long"))["close_ts"].to_list())
    assert gated["signal_long"].sum() < free["signal_long"].sum()
    # без ряда широты фильтр закрыт
    closed = get_strategy("feature_rules", {**base, "breadth_min_frac": 0.75}).compute(
        resample(bars[s], "1h"), hh_symbol=s, tf="1h", bars_1m=bars[s])
    assert not closed["signal_long"].any() and not closed["signal_short"].any()


def test_engine_passes_breadth(market, no_ext, monkeypatch):
    from quant.backtest import engine
    from quant.data import store

    insts, bars = market
    monkeypatch.setattr(store, "load_bars", lambda sym, *a, **k: bars.get(sym, pl.DataFrame()))
    monkeypatch.setattr(I, "instruments", lambda *a, **k: list(insts.values()))
    monkeypatch.setattr(engine, "_btc_trend", lambda *a, **k: None)
    ov = {"rules_long": RULE_L, "rules_short": [], "rearm_bars": 6}
    spec = lambda o: engine.BuildSpec(strategy="feature_rules", symbols=["SOLUSDT"], start=START, end=END,  # noqa: E731
                                      overrides=o, record_path=False, use_book_depth=False)
    free = engine.build_tradeset(spec(ov))
    gated = engine.build_tradeset(spec({**ov, "breadth_min_frac": 0.75}))
    assert 0 < gated.trades.height < free.trades.height
    b = B.breadth_series(list(insts.values()), START, END, tf="1h", htf_tf="4h", rules_long=RULE_L, rules_short=[],
                         load_bars=lambda inst: bars[inst.hh_symbol], warmup_days=engine.WARMUP_DAYS)
    ok = set(b.filter(pl.col("frac_long") >= 0.75)["close_ts"].to_list())
    assert set(gated.trades["signal_ts"].to_list()) <= ok               # каждая сделка — в час широты >= порога
    assert len(B._CACHE) == 1                                            # ряд посчитан один раз и закэширован
