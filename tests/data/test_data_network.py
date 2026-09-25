"""Сетевые тесты фазы 2 (QUANT_NETWORK_TESTS=1): реальные архив и REST Binance/Bybit."""

from __future__ import annotations

import hashlib

import httpx
import polars as pl
import pytest
from sqlalchemy import select

from quant.data import store
from quant.data.archive import BinanceArchive
from quant.data.backfill import backfill, symbol_start
from quant.data.build import bars_frame, build_symbol
from quant.data.flags import halted_runs
from quant.data.validate import validate
from quant.db import session_scope
from quant.db.models import ArchiveFile
from quant.instruments import by_symbol
from quant.timeutil import utc

pytestmark = pytest.mark.network


def test_litusdt_history_start_and_frozen_period(tmp_settings):
    """LITUSDT: Litentry → «замороженные» бары → Lighter с 2025-12-23 17:30 UTC."""
    inst = by_symbol()["LITUSDT"]
    rep = backfill([inst], ["klines_1m"], utc(2025, 11, 1), utc(2026, 1, 1), workers=4,
                   rest_tail=False, clip_history=False)
    assert rep["archive"]["failed"] == 0 and rep["archive"]["downloaded"] == 2
    raw = bars_frame(inst, cut=False)
    assert raw["ts"].min() == utc(2025, 11, 1)                          # raw — как есть, до history_start
    runs = halted_runs(raw, 60)
    frozen = runs.filter(pl.col("minutes") >= 30 * 1440)
    assert frozen.height == 1
    assert frozen["start_ts"][0] <= utc(2025, 11, 1) and frozen["end_ts"][0] >= utc(2025, 12, 22, 23, 0)
    assert frozen["end_ts"][0] < inst.history_start
    build_symbol("LITUSDT", ["bars_1m"])
    b = store.load_bars("LITUSDT", with_mark=False)
    assert b["ts"].min() == utc(2025, 12, 23, 17, 30)                   # до history_start ничего нет
    assert store.load_bars("LITUSDT", utc(2025, 1, 1), inst.history_start, with_mark=False).is_empty()
    assert not b["halted"].any()


def test_pumpusdt_history_start(tmp_settings):
    """PUMPUSDT: другой актив 2025-04…07, pump.fun с 2025-07-10 07:30 UTC."""
    inst = by_symbol()["PUMPUSDT"]
    rep = backfill([inst], ["klines_1m"], utc(2025, 6, 1), utc(2025, 8, 1), workers=4,
                   rest_tail=False, clip_history=False)
    assert rep["archive"]["failed"] == 0
    raw = bars_frame(inst, cut=False)
    assert raw["ts"].min() < inst.history_start
    build_symbol("PUMPUSDT", ["bars_1m"])
    b = store.load_bars("PUMPUSDT", with_mark=False)
    assert b["ts"].min() == utc(2025, 7, 10, 7, 30)
    assert b.filter(pl.col("ts") < inst.history_start).is_empty()
    # по умолчанию (clip_history) backfill раньше history_start не качает
    assert symbol_start(inst, utc(2025, 1, 1)) == inst.history_start


def test_real_checksum_verification(tmp_settings):
    arch = BinanceArchive(workers=2)
    files = arch.plan("fundingRate", "BTCUSDT", utc(2020, 1, 1), utc(2020, 3, 1))
    assert [f.period for f in files] == ["2020-01", "2020-02"]
    s = arch.sync(files)
    assert (s.downloaded, s.failed) == (2, 0)
    # независимая проверка: sha256 zip == .CHECKSUM == манифест
    zdata = httpx.get(arch.url(files[0]), timeout=60).content
    expected = arch.fetch_checksum(files[0])
    assert hashlib.sha256(zdata).hexdigest() == expected
    with session_scope() as ss:
        rows = ss.scalars(select(ArchiveFile).order_by(ArchiveFile.period)).all()
        assert all(r.checksum_ok for r in rows) and rows[0].sha256 == expected
        assert rows[0].rows == pl.read_parquet(tmp_settings.data_dir / rows[0].raw_path).height
    assert arch.sync(files, recheck=True).skipped == 2


def test_validate_live_subset(tmp_settings):
    bs = by_symbol()
    items = [bs[s] for s in ("BTCUSDT", "PEPEUSDT", "SHIBUSDT", "BEAMXUSDT", "XAUUSDT", "XPTUSDT")]
    res = validate(items)
    bad = [(r.hh_symbol, [(i.code, i.detail) for i in r.issues]) for r in res if not r.ok]
    assert not bad, bad
