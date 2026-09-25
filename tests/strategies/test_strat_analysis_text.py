"""Шаблонный разбор quant.analysis.describe: 3–6 предложений, детерминированно, только из входа."""

from __future__ import annotations

import polars as pl
from strat_helpers import SYMBOL, make_strategy

from quant.analysis import describe, fmt_price
from quant.strategies.base import Factor


def F(name, label, pts, mx, verdict):
    return Factor(name, label, None, pts, mx, verdict)


LONG = [F("htf", "HTF аптренд (EMA200 4H)", 25, 25, "pro"), F("rsi", "RSI 44↑", 10, 10, "pro"),
        F("vwap", "Цена выше VWAP, откат к AVWAP", 15, 15, "pro"), F("sweep", "Sweep ликвидности нет", 0, 10, "neutral"),
        F("adx", "ADX 19 — тренд слабый", 0, 0, "con")]
SHORT = [F("htf", "HTF аптренд (EMA200 4H)", 0, 25, "con"), F("rsi", "RSI 44↑ — импульс вверх", 0, 10, "con"),
         F("vwap", "Цена выше VWAP", 0, 15, "con"), F("sweep", "Sweep ликвидности нет", 0, 10, "neutral")]


def test_basic_structure_and_determinism():
    out = describe(LONG, SHORT, {"hh_symbol": "SOLUSDT", "tf": "1h"})
    assert 3 <= len(out) <= 6
    assert out == describe(LONG, SHORT, {"hh_symbol": "SOLUSDT", "tf": "1h"})
    assert out[0].startswith("SOLUSDT 1H: score лонга 83, шорта 0") and "перевес у лонга" in out[0]
    assert out[1] == "За лонг: HTF аптренд (EMA200 4H), цена выше VWAP, откат к AVWAP, RSI 44↑."
    assert out[2] == "Против лонга: ADX 19 — тренд слабый."
    assert out[3].startswith("Для шорта: против — ")
    assert all(s.endswith(".") for s in out)


def test_no_claims_without_data():
    out = describe(LONG, SHORT, {})
    text = " ".join(out)
    for word in ("BTC", "сессия", "funding", "сигнал"):
        assert word not in text
    # BTC — только для альтов, сессия — только для TradFi
    assert "BTC" not in " ".join(describe(LONG, SHORT, {"btc_trend": 1}))
    assert describe(LONG, SHORT, {"btc_trend": 1, "is_alt": True})[-1] == "Тренд BTC восходящий."
    assert "сессия" not in " ".join(describe(LONG, SHORT, {"session_open": False}))
    t = describe(LONG, SHORT, {"is_tradfi": True, "session_open": False, "session_next_change": "13:30 UTC"})
    assert t[-1] == "Сессия базового рынка закрыта (откроется 13:30 UTC)."


def test_context_and_signal_cap_at_six():
    ctx = {"hh_symbol": "SOLUSDT", "tf": "1h", "is_alt": True, "btc_trend": -1, "funding_rate": 0.0007,
           "signal": {"side": "long", "entry": 142.35, "sl": 139.8, "tp1": 146.1, "tp2": 149.4}}
    out = describe(LONG, SHORT, ctx)
    assert len(out) == 6
    assert out[4] == "Тренд BTC нисходящий; funding +0.0700% — высокий, лонги платят шортам."
    assert out[5] == "Активный сигнал: лонг, вход 142.35, SL 139.8, TP1 146.1, TP2 149.4."


def test_empty_and_balanced():
    out = describe([], [], None)
    assert len(out) == 3 and "не рассчитан" in out[0]
    even = describe(LONG, LONG, {"score_long": 50, "score_short": 45})
    assert "силы сторон близки" in even[0]


def test_fmt_price():
    assert fmt_price(142.35) == "142.35"
    assert fmt_price(64250.5) == "64250.5"
    assert fmt_price(0.0012345) == "0.0012345"
    assert fmt_price(0.00001234) == "0.00001234"


def test_describe_on_strategy_output(bars_1h, bars_4h):
    s = make_strategy("confluence_v1")
    fr = s.compute(bars_1h, hh_symbol=SYMBOL, tf="1h", htf=bars_4h)
    row = fr.filter(pl.col("signal_short")).row(0, named=True)
    out = describe(s.explain(row, "long"), s.explain(row, "short"),
                   {"hh_symbol": SYMBOL, "tf": "1h", "score_long": row["score_long"], "score_short": row["score_short"]})
    assert 3 <= len(out) <= 6 and "перевес у шорта" in out[0]
