"""Паритет live/бэктест на уровне баров: ТФ, собранные в live по одному 1m-бару, совпадают с пакетной агрегацией."""

from __future__ import annotations

import polars as pl
from polars.testing import assert_frame_equal

from quant.backtest.synthetic import random_walk_bars
from quant.data.aggregate import resample
from quant.instruments import get
from signals.state import SymbolState, on_bar


def test_incremental_tf_bars_equal_batch():
    bars = random_walk_bars(3 * 1440, seed=9).drop(["m_open", "m_high", "m_low", "m_close"])
    half = bars.head(1440)
    st = SymbolState(inst=get("BTCUSDT"), m1=half)
    for tf in ("15m", "1h", "4h", "1d"):
        st.bars[tf] = resample(half, tf)
    closed_count = {}
    for row in bars.slice(1440).iter_rows(named=True):
        msg = {**row, "ts": int(row["ts"].timestamp() * 1000), "hh_symbol": "BTCUSDT"}
        for tf in on_bar(st, msg):
            closed_count[tf] = closed_count.get(tf, 0) + 1
    assert closed_count["1h"] == 48 and closed_count["4h"] == 12 and closed_count["1d"] == 2
    cols = ["ts", "open", "high", "low", "close", "volume", "taker_buy_volume"]
    for tf in ("15m", "1h", "4h"):
        batch = resample(bars, tf).select(cols)
        live = st.bars[tf].select(cols)
        assert_frame_equal(live.tail(len(live)), batch.tail(len(live)), check_dtypes=False)
