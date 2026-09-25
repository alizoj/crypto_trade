"""Валидация инструментов (3.1.5) на фейковых ответах бирж."""

from __future__ import annotations

import pytest

from quant.data.validate import format_report, median_divergence_pct, symbol_multiplier, validate
from quant.db import session_scope
from quant.db.models import InstrumentStatus
from quant.instruments import by_symbol
from quant.timeutil import to_ms


def _ms(dt) -> int:
    return to_ms(dt)


class FakeBinance:
    def __init__(self, items, *, prices=None, overrides=None, spot_status=None):
        self.syms = {}
        for i in items:
            base = i.binance_um.removesuffix("USDT")
            self.syms[i.binance_um] = {"symbol": i.binance_um, "status": "TRADING", "baseAsset": base,
                                       "onboardDate": _ms(i.binance_onboard), "underlyingType": i.underlying_type}
        for sym, patch in (overrides or {}).items():
            self.syms[sym].update(patch)
        self.prices = prices or {}
        self.spot = {i.binance_spot: {"symbol": i.binance_spot, "status": (spot_status or {}).get(i.binance_spot,
                                                                                                 "TRADING")}
                     for i in items if i.binance_spot}

    def exchange_info(self):
        return {"symbols": list(self.syms.values())}

    def spot_exchange_info(self):
        return {"symbols": list(self.spot.values())}

    def klines(self, symbol, start_ms, end_ms, **kw):
        p = self.prices.get(symbol, 100.0)
        return [[t, "0", "0", "0", str(p), "0"] for t in range(start_ms, end_ms, 60_000)]

    def close(self):
        pass


class FakeBybit:
    def __init__(self, items, *, prices=None, overrides=None, closed=None):
        self.syms = {i.bybit_linear: {"symbol": i.bybit_linear, "status": "Trading",
                                      "launchTime": str(_ms(i.bybit_launch))} for i in items if i.bybit_linear}
        for sym, patch in (overrides or {}).items():
            self.syms[sym].update(patch)
        self.closed = closed or {}
        self.prices = prices or {}
        self.by_symbol_calls = []

    def instruments(self, *, category="linear", symbol=None, status=None):
        if symbol:
            self.by_symbol_calls.append(symbol)
            return [self.closed[symbol]] if symbol in self.closed else []
        return [s for s in self.syms.values() if s["symbol"] not in self.closed]

    def kline(self, symbol, start_ms, end_ms, **kw):
        p = self.prices.get(symbol, 100.0)
        return [[str(t), "0", "0", "0", str(p), "0", "0"] for t in range(start_ms, end_ms, 60_000)]

    def close(self):
        pass


def _items(*names):
    bs = by_symbol()
    return [bs[n] for n in names]


def test_symbol_multiplier():
    assert symbol_multiplier("1000PEPE") == 1000
    assert symbol_multiplier("1000PEPEUSDT") == 1000
    assert symbol_multiplier("SHIB1000USDT") == 1000
    assert symbol_multiplier("1MBABYDOGE") == 1_000_000
    assert symbol_multiplier("1000000MOGUSDT") == 1_000_000
    assert symbol_multiplier("1INCH") == 1 and symbol_multiplier("1INCHUSDT") == 1
    assert symbol_multiplier("BTCUSDT") == 1 and symbol_multiplier("EWJUSDT") == 1


def test_all_ok_and_status_persisted(tmp_settings):
    items = _items("BTCUSDT", "PEPEUSDT", "SHIBUSDT", "XPTUSDT")
    b = FakeBinance(items, overrides={"1000PEPEUSDT": {"baseAsset": "1000PEPE"},
                                      "1000SHIBUSDT": {"baseAsset": "1000SHIB"}})
    res = validate(items, binance=b, bybit=FakeBybit(items))
    assert all(r.ok for r in res), format_report(res)
    assert [r.divergence_median_pct for r in res][:3] == [0.0, 0.0, 0.0]
    assert res[3].divergence_median_pct is None                               # XPT — нет на Bybit
    with session_scope() as s:
        st = s.get(InstrumentStatus, "PEPEUSDT")
        assert st.ok and not st.signals_blocked and st.divergence_median_pct == 0.0


def test_onboard_changed_blocks_signals(tmp_settings):
    items = _items("LITUSDT", "PUMPUSDT")
    b = FakeBinance(items, overrides={"LITUSDT": {"onboardDate": _ms(by_symbol()["LITUSDT"].binance_onboard) + 3600_000}})
    y = FakeBybit(items, overrides={"PUMPFUNUSDT": {"launchTime": "1700000000000"}})
    res = {r.hh_symbol: r for r in validate(items, binance=b, bybit=y)}
    assert res["LITUSDT"].signals_blocked and [i.code for i in res["LITUSDT"].issues] == ["onboard_changed"]
    assert res["PUMPUSDT"].signals_blocked and "Bybit launchTime" in res["PUMPUSDT"].issues[0].detail
    with session_scope() as s:
        st = s.get(InstrumentStatus, "LITUSDT")
        assert st.signals_blocked and not st.ok and st.issues[0]["code"] == "onboard_changed"


def test_multiplier_and_status_issues(tmp_settings):
    items = _items("PEPEUSDT", "ETHUSDT", "FETUSDT")
    b = FakeBinance(items, overrides={"1000PEPEUSDT": {"baseAsset": "PEPE"}, "ETHUSDT": {"status": "SETTLING"}},
                    spot_status={"FETUSDT": "BREAK"})
    y = FakeBybit(items, closed={"ETHUSDT": {"symbol": "ETHUSDT", "status": "Closed",
                                             "launchTime": str(_ms(by_symbol()["ETHUSDT"].bybit_launch))}})
    res = {r.hh_symbol: r for r in validate(items, binance=b, bybit=y)}
    assert any(i.code == "multiplier_mismatch" for i in res["PEPEUSDT"].issues) and res["PEPEUSDT"].signals_blocked
    codes = {i.code for i in res["ETHUSDT"].issues}
    assert {"binance_not_trading", "bybit_not_trading"} <= codes
    assert "ETHUSDT" in y.by_symbol_calls                                     # закрытый запрошен по symbol=
    assert res["FETUSDT"].issues[0].code == "spot_not_trading" and res["FETUSDT"].ok   # только warn


@pytest.mark.parametrize("hh,pct,suspect", [
    ("BTCUSDT", 0.25, False), ("BTCUSDT", 0.35, True),      # crypto_major: 0.3%
    ("DUSKUSDT", 0.45, False), ("DUSKUSDT", 0.7, True),     # crypto_alt: 0.6%
    ("TSLAUSDT", 0.35, True),                                # tradfi: 0.3%
])
def test_divergence_thresholds_by_group(tmp_settings, hh, pct, suspect):
    items = _items(hh)
    i = items[0]
    b = FakeBinance(items, prices={i.binance_um: 100.0})
    y = FakeBybit(items, prices={i.bybit_linear: 100.0 * (1 + pct / 100)})
    r = validate(items, binance=b, bybit=y)[0]
    assert r.divergence_median_pct == pytest.approx(pct)
    assert r.mapping_suspect is suspect and (r.ok is not suspect)


def test_median_divergence_normalized():
    b = [[0, "0", "0", "0", "0.0105"], [60000, "0", "0", "0", "0.0106"]]
    y = [[0, "0", "0", "0", "10.5"], [60000, "0", "0", "0", "10.6"], [120000, "0", "0", "0", "99"]]
    assert median_divergence_pct(b, y, 1000, 1_000_000) == pytest.approx(0.0, abs=1e-9)
    assert median_divergence_pct([], y, 1, 1) is None
