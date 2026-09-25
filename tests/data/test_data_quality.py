"""Реестр гэпов и причины (3.2.3), halted-записи, отчёт качества, догрузка через REST."""

from __future__ import annotations

from datetime import timedelta

import polars as pl

from _datahelpers import kline_lines, write_raw_klines
from quant.data import quality, store
from quant.data.build import build_symbol
from quant.data.gapfill import fill_gaps
from quant.timeutil import to_ms, utc

T0 = utc(2024, 4, 1)


def _put(hh: str, source_symbol: str, lines: list[str]) -> None:
    write_raw_klines(source_symbol, "2024-04", lines)
    build_symbol(hh, ["bars_1m"])


def _drop(lines: list[str], start: int, n: int) -> list[str]:
    return lines[:start] + lines[start + n:]


def test_gap_reasons_local(tmp_settings):
    # LITUSDT: history_start 2025-12-23 17:30. Первый бар позже → listing; дальше — unknown и relisting.
    hs = utc(2025, 12, 23, 17, 30)
    lines = kline_lines(hs + timedelta(minutes=5), 300)
    lines = _drop(lines, 100, 3)                                            # обычный пропуск → unknown
    frozen = kline_lines(hs + timedelta(minutes=305), 80, frozen=True, price=4.0)
    after = kline_lines(hs + timedelta(minutes=305 + 80 + 30), 20, price=4.0)   # 30 мин пропуск после halted
    write_raw_klines("LITUSDT", "2025-12", lines + frozen + after)
    build_symbol("LITUSDT", ["bars_1m"])
    gaps = quality.scan_symbol("LITUSDT").gaps
    by = {(g.reason, g.start): g for g in gaps}
    assert by[("listing", hs)].minutes == 5                                 # от history_start до первого бара
    assert by[("halted", hs + timedelta(minutes=305))].minutes == 80
    assert by[("relisting", hs + timedelta(minutes=385))].minutes == 30
    # пропуск 3 минуты в первые сутки после history_start — окно listing
    assert by[("listing", hs + timedelta(minutes=105))].minutes == 3
    assert sorted(g.reason for g in gaps) == ["halted", "listing", "listing", "relisting"]
    gaps2 = quality.scan_symbol("LITUSDT", listing_window_min=60).gaps
    unk = [g for g in gaps2 if g.reason == "unknown"]
    assert len(unk) == 1 and unk[0].minutes == 3 and unk[0].start == hs + timedelta(minutes=105)


def test_exchange_maintenance_heuristic(tmp_settings):
    base = kline_lines(T0, 600)
    for hh in ("BTCUSDT", "ETHUSDT", "SOLUSDT", "DOGEUSDT"):
        _put(hh, hh, _drop(base, 200, 10))                                 # одновременный гэп у всех
    _put("LINKUSDT", "LINKUSDT", _drop(base, 400, 7))                      # одиночный гэп
    gaps = quality.detect_gaps(["BTCUSDT", "LINKUSDT"])
    assert [(g.reason, g.minutes) for g in gaps["BTCUSDT"]] == [("exchange_maintenance", 10)]
    assert "4/5" in gaps["BTCUSDT"][0].detail
    assert [(g.reason, g.minutes) for g in gaps["LINKUSDT"]] == [("unknown", 7)]
    # порог 80%: при share=0.9 четыре из пяти уже не maintenance
    g2 = quality.detect_gaps(["BTCUSDT"], maintenance_share=0.9)
    assert g2["BTCUSDT"][0].reason == "unknown"


def test_source_outage_in_live_part(tmp_settings):
    _put("BTCUSDT", "BTCUSDT", kline_lines(T0, 60))
    live_ts = [T0 + timedelta(minutes=m) for m in (60, 61, 70, 71)]
    df = pl.DataFrame({"ts": live_ts, "open": [1.0] * 4, "high": [1.0] * 4, "low": [1.0] * 4, "close": [1.0] * 4,
                       "volume": [1.0] * 4}).with_columns(pl.col("ts").cast(pl.Datetime("ms", "UTC")))
    store.write_live_bars("BTCUSDT", df)
    g = quality.scan_symbol("BTCUSDT").gaps
    assert [(x.reason, x.start, x.minutes) for x in g] == [("source_outage", T0 + timedelta(minutes=62), 8)]


def test_registry_sync_and_report(tmp_settings):
    lines = kline_lines(T0, 300)
    _put("ETHUSDT", "ETHUSDT", _drop(lines, 100, 5))
    stats = quality.register_gaps(quality.detect_gaps(["ETHUSDT"]))
    assert stats["added"] == 1
    reg = quality.load_registry(["ETHUSDT"])
    assert [(r["reason"], r["minutes"], r["filled"]) for r in reg] == [("unknown", 5, False)]
    rep = quality.quality_report("ETHUSDT")
    assert rep["unknown_gaps"] == 1 and rep["missing_min"] == 5 and rep["gaps"]["unknown"]["minutes"] == 5
    assert rep["halted_pct"] == 0 and rep["synthetic_pct"] == 0
    # повторная регистрация — обновление, без дублей
    assert quality.register_gaps(quality.detect_gaps(["ETHUSDT"]))["updated"] == 1
    # данные появились → запись filled
    write_raw_klines("ETHUSDT", "2024-04-01", lines[100:105], dataset="rest_klines_1m")
    build_symbol("ETHUSDT", ["bars_1m"])
    assert quality.register_gaps(quality.detect_gaps(["ETHUSDT"]))["closed"] == 1
    assert quality.load_registry(["ETHUSDT"])[0]["filled"] is True
    assert quality.quality_report("ETHUSDT")["unknown_gaps"] == 0


def test_halted_records_and_fraction(tmp_settings):
    lines = kline_lines(T0, 100) + kline_lines(T0 + timedelta(minutes=100), 120, frozen=True, price=5) + \
        kline_lines(T0 + timedelta(minutes=220), 80)
    _put("SOLUSDT", "SOLUSDT", lines)
    rep = quality.run_quality(["SOLUSDT"])[0]
    assert rep["gaps"]["halted"] == {"count": 1, "minutes": 120}
    assert abs(rep["halted_pct"] - 100 * 120 / 300) < 1e-9
    assert rep["zero_vol_pct"] >= rep["halted_pct"]


class FakeREST:
    """Отдаёт бары только для минут из available (сек. эпохи), mark — те же."""

    def __init__(self, available: dict[int, list]):
        self.available = available
        self.calls: list[tuple] = []

    def klines(self, symbol, start_ms, end_ms, *, kind="klines", **kw):
        self.calls.append((symbol, start_ms, end_ms, kind))
        return [r for t, r in sorted(self.available.items()) if start_ms <= t < end_ms]

    def close(self):
        pass


def test_fill_gaps_rest_version2_and_confirmed_empty(tmp_settings):
    lines = kline_lines(T0, 400)
    _put("DOGEUSDT", "DOGEUSDT", _drop(_drop(lines, 300, 4), 100, 6))      # гэпы 100..105 и 300..303
    # REST знает только первый гэп
    avail = {}
    for ln in lines[100:106]:
        parts = ln.split(",")
        avail[int(parts[0])] = parts
    rest = FakeREST(avail)
    rep = fill_gaps(["DOGEUSDT"], rest=rest)
    assert rep["gaps"] == 2 and rep["bars"] == 6 and rep["filled"] == 1 and rep["confirmed_empty"] == 1
    assert {c[3] for c in rest.calls} == {"klines", "markPriceKlines"}
    b = store.load_bars("DOGEUSDT", with_mark=False)
    filled = b.filter((pl.col("ts") >= T0 + timedelta(minutes=100)) & (pl.col("ts") < T0 + timedelta(minutes=106)))
    assert filled.height == 6 and (filled["version"] == 2).all() and not b["synthetic"].any()
    reg = {r["start_ts"]: r for r in quality.load_registry(["DOGEUSDT"])}
    assert reg[T0 + timedelta(minutes=100)]["filled"] is True
    left = reg[T0 + timedelta(minutes=300)]
    assert left["filled"] is False and left["reason"] == "exchange_maintenance" and "REST:" in left["detail"]
    # повторный детект не откатывает подтверждённую причину к unknown
    quality.register_gaps(quality.detect_gaps(["DOGEUSDT"]))
    assert quality.load_registry(["DOGEUSDT"], open_only=True)[0]["reason"] == "exchange_maintenance"
    assert to_ms(T0) > 0


def test_live_vs_archive_t_plus_1(tmp_settings):
    lines = kline_lines(T0, 10)
    _put("AVAXUSDT", "AVAXUSDT", lines)
    b = store.load_bars("AVAXUSDT", with_mark=False)
    live = b.with_columns(pl.when(pl.col("ts") == T0 + timedelta(minutes=3)).then(pl.col("close") * 1.01)
                          .otherwise(pl.col("close")).alias("close"))
    extra = b.tail(1).with_columns(pl.col("ts") + pl.duration(minutes=1))
    store.write_live_bars("AVAXUSDT", pl.concat([live, extra]))
    rep = quality.live_vs_archive("AVAXUSDT")
    assert (rep["compared"], rep["mismatched"], rep["live_only"]) == (10, 1, 1)
    assert rep["examples"][0]["ts"] == T0 + timedelta(minutes=3)
    # при чтении архив перекрывает расходящуюся live-минуту
    got = store.load_bars("AVAXUSDT", with_mark=False).filter(pl.col("ts") == T0 + timedelta(minutes=3))
    assert got["version"][0] == 3 and got["close"][0] == b.filter(pl.col("ts") == T0 + timedelta(minutes=3))["close"][0]


def test_mark_gaps_registered_and_verified_by_rest(tmp_settings):
    """Одновременная дыра в архиве mark у всех символов — эвристика даёт maintenance, REST её опровергает."""
    lines = kline_lines(T0, 300)
    for hh in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        write_raw_klines(hh, "2024-04", lines)
        write_raw_klines(hh, "2024-04", _drop(lines, 100, 20), dataset="markPriceKlines_1m")
        build_symbol(hh, ["bars_1m", "mark_1m"])
    gaps = quality.detect_gaps(["BTCUSDT"])
    mark = [g for g in gaps["BTCUSDT"] if g.dataset == quality.MARK_DATASET]
    assert [(g.reason, g.minutes) for g in mark] == [("exchange_maintenance", 20)]
    avail = {int(ln.split(",")[0]): ln.split(",") for ln in lines[100:120]}
    rep = fill_gaps(["BTCUSDT", "ETHUSDT", "SOLUSDT"], rest=FakeREST(avail))
    assert rep["gaps"] == 3 and rep["filled"] == 3 and rep["confirmed_empty"] == 0
    m = store.load_mark("BTCUSDT")
    assert m.height == 300
    assert quality.quality_report("BTCUSDT")["mark_gaps"] == {}


def test_agg_trades_continuity_by_ids(tmp_settings):
    from _datahelpers import write_raw

    rows = [(1, 10, 10), (2, 11, 12), (3, 13, 13), (5, 16, 16)]            # пропущены agg_id=4 и trade_id 14..15
    csv = "".join(f"{a},100,1,{f},{l},{to_ms(T0) + a},true\n" for a, f, l in rows)
    write_raw("agg_trades", "aggTrades", "BTCUSDT", "2024-04", csv)
    build_symbol("BTCUSDT", ["agg_trades"])
    rep = quality.agg_trades_continuity("BTCUSDT", T0, T0 + timedelta(days=1))
    assert (rep["trades"], rep["agg_id_gaps"], rep["agg_id_missing"], rep["trade_id_gaps"]) == (4, 1, 1, 1)
