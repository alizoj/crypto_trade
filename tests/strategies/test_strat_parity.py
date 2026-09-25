"""Паритет live/бэктест (раздел 6, приёмка фазы 6): on_bar(Context) на скользящем окне
с прогревом warmup_bars() даёт те же сигналы, что compute на полных данных, на тех же барах.

Context: bars — последние warmup_bars() закрытых баров ТФ; htf — все закрытые к этому моменту бары
HTF (EMA200 на HTF сходится только за 25×200 баров HTF, поэтому HTF в live держится целиком);
meta — внешние ряды, известные к моменту закрытия бара.
"""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest
from strat_helpers import SYMBOL, cut_meta, make_strategy

from quant.data.aggregate import closed_bars
from quant.strategies.base import Context, Signal

CASES = [("confluence_v1", False), ("confluence_v1", True), ("baseline_random", False), ("rsi_pin_ew", False), ("feature_rules", False)]
IDS = ["confluence", "confluence+filters", "baseline", "rsi_pin_ew", "feature_rules"]


def _pick_bars(full: pl.DataFrame, start: int, n_random: int, seed: int) -> list[int]:
    sig = np.flatnonzero((full["signal_long"] | full["signal_short"]).to_numpy())
    sig = sig[sig >= start]
    rng = np.random.default_rng(seed)
    rnd = rng.integers(start, full.height, n_random)
    return sorted(set(sig[:25].tolist()) | set(rnd.tolist()))


def _expected(s, full: pl.DataFrame, i: int) -> list[dict]:
    return s.signals_from(full.slice(i, 1), hh_symbol=SYMBOL, tf="1h").to_dicts()


@pytest.mark.parametrize("name,filters", CASES, ids=IDS)
def test_on_bar_matches_compute(bars_1h, bars_4h, meta, name, filters):
    s = make_strategy(name, filters)
    full = s.compute(bars_1h, hh_symbol=SYMBOL, tf="1h", htf=bars_4h, meta=meta)
    w = s.warmup_bars()
    idx = _pick_bars(full, w, 15, seed=4)
    n_sig = 0
    for i in idx:
        now = int(full["close_ts"].dt.epoch("ms")[i])
        ctx = Context(SYMBOL, "1h", bars_1h.slice(i - w + 1, w), htf=closed_bars(bars_4h, "4h", now),
                      bars_1m=None, meta=cut_meta(meta, now + 1))
        got: list[Signal] = s.on_bar(ctx)
        exp = _expected(s, full, i)
        assert len(got) == len(exp), f"бар {i}: on_bar {len(got)} сигн., compute {len(exp)}"
        for g, e in zip(sorted(got, key=lambda x: x.side), sorted(exp, key=lambda x: -x["side"])):
            n_sig += 1
            assert g.side == ("long" if e["side"] == 1 else "short")
            assert g.ts == e["ts"]
            for a, b in ((g.entry_price, e["entry_price"]), (g.sl, e["sl"]), (g.tp1, e["tp1"]),
                         (g.score, e["score"])):
                assert a == pytest.approx(b, rel=1e-9)
            assert (g.tp2 is None) == (e["tp2"] is None)
            if name == "confluence_v1":
                assert g.reason["pros"], "у сигнала confluence должны быть факторы «за»"
    assert n_sig > 5


def test_on_bar_uses_only_window(bars_1h, bars_4h):
    """Context не даёт будущих баров: последний бар окна = текущий, on_bar смотрит только на него."""
    s = make_strategy("confluence_v1")
    i = 2000
    ctx = Context(SYMBOL, "1h", bars_1h.slice(i - 800, 800), htf=bars_4h, bars_1m=None)
    frame = s.compute(ctx.bars, hh_symbol=SYMBOL, tf="1h", htf=ctx.htf)
    assert frame["ts"][-1] == bars_1h["ts"][i - 1]
    empty = Context(SYMBOL, "1h", bars_1h.head(0))
    assert s.on_bar(empty) == []
