"""Данные рынка для API: из сервиса сигналов (локальный режим, один процесс) или из хранилища/шины (прод)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from quant import instruments as I
from quant.bus import Bus
from quant.data import store
from quant.data.aggregate import resample
from quant.indicators import core
from quant.indicators import volume as vol
from quant.indicators.frame import add_indicators
from quant.strategies import get_strategy, load_params, with_overrides
from quant.timeutil import now_utc, tf_minutes
from signals.state import HTF_OF, SymbolState, warmup

_CACHE_TTL = 60


class MarketView:
    def __init__(self, bus: Bus, service: Any | None = None, strategy: str = "confluence_v1"):
        self.bus = bus
        self.service = service
        self.strategy = strategy
        self._cache: dict[str, tuple[float, SymbolState]] = {}

    async def screener(self, tf: str) -> dict:
        if self.service is not None:
            rows = sorted(self.service.screener.get(tf, {}).values(), key=lambda r: r["hh_symbol"])
            return {"tf": tf, "updated_at": now_utc().strftime("%Y-%m-%dT%H:%M:%SZ"), "rows": rows}
        snap = await self.bus.get_state(f"screener:{tf}")
        return snap or {"tf": tf, "updated_at": None, "rows": []}

    def symbol_state(self, sym: str) -> SymbolState:
        if self.service is not None and sym in self.service.state:
            return self.service.state[sym]
        hit = self._cache.get(sym)
        if hit and time.time() - hit[0] < _CACHE_TTL:
            return hit[1]
        st = warmup(I.get(sym))
        self._cache[sym] = (time.time(), st)
        return st

    def marks(self) -> dict[str, float]:
        if self.service is not None:
            return {s: (st.last_mark if st.last_mark is not None else st.price)
                    for s, st in self.service.state.items() if (st.last_mark or st.price) is not None}
        out = {}
        for sym, (_, st) in self._cache.items():
            if st.price is not None:
                out[sym] = st.price
        return out

    def bars_payload(self, sym: str, tf: str, limit: int, end: datetime | None) -> dict:
        st = self.symbol_state(sym)
        bars = st.bars.get(tf)
        if bars is None or bars.is_empty():
            return {"tf": tf, "bars": [], "indicators": {}, "volume_profile": None, "markers": []}
        if end is not None:
            bars = bars.filter(pl.col("ts") < end)
        ind = add_indicators(bars.tail(max(limit + 250, 450)))
        view = ind.tail(limit)
        t = (view["ts"].dt.epoch("ms") // 1000).to_list()

        def series(col: str) -> list[dict]:
            if col not in view.columns:
                return []
            return [{"t": a, "v": float(b)} for a, b in zip(t, view[col].to_list()) if b is not None and b == b]

        o, c = view["open"].to_list(), view["close"].to_list()
        vols = view["volume"].fill_null(0).to_list()
        # AVWAP от последнего подтверждённого свинга (из кадра стратегии, если есть)
        avwap = []
        frame = self._frame(st, tf)
        if frame is not None and not frame.is_empty():
            fr = frame.join(view.select("ts"), on="ts", how="inner")
            for col in ("avwap_long", "avwap"):
                if col in fr.columns:
                    tt = (fr["ts"].dt.epoch("ms") // 1000).to_list()
                    avwap = [{"t": a, "v": float(b)} for a, b in zip(tt, fr[col].to_list()) if b is not None and b == b]
                    break
        vp = vol.volume_profile(view["high"].to_numpy(), view["low"].to_numpy(), view["volume"].fill_null(0).to_numpy())
        rsi_col = next((col for col in view.columns if col.startswith("rsi")), None)
        return {
            "tf": tf,
            "bars": [{"t": a, "o": float(x), "h": float(hh), "l": float(ll), "c": float(cc), "v": float(vv)}
                     for a, x, hh, ll, cc, vv in zip(t, o, view["high"].to_list(), view["low"].to_list(), c, vols)],
            "indicators": {
                "ema20": series("ema20"), "ema50": series("ema50"), "ema200": series("ema200"),
                "vwap": series("vwap"), "avwap": avwap, "rsi": series(rsi_col) if rsi_col else [],
                "cvd": series("cvd"),
                "volume": [{"t": a, "v": float(vv), "up": bool(cc >= x)} for a, vv, cc, x in zip(t, vols, c, o)],
            },
            "volume_profile": None if vp is None else {
                "poc": float(vp.poc), "vah": float(vp.vah), "val": float(vp.val),
                "bins": [{"price": float(p), "volume": float(v)} for p, v in zip(vp.centers, vp.volume)]},
        }

    def _frame(self, st: SymbolState, tf: str) -> pl.DataFrame | None:
        bars = st.bars.get(tf)
        if bars is None or bars.height < 50:
            return None
        params = with_overrides(load_params(self.strategy, st.inst.group), {"tf": tf, "htf": HTF_OF.get(tf, "4h")})
        strat = get_strategy(self.strategy, params)
        return strat.compute(bars, hh_symbol=st.inst.hh_symbol, tf=tf, htf=st.bars.get(HTF_OF.get(tf, "4h")),
                             bars_1m=st.m1.tail(1440),
                             meta={"instrument": st.inst, "funding": st.funding, "metrics": st.metrics})

    def last_row(self, sym: str, tf: str) -> tuple[dict | None, Any, dict]:
        st = self.symbol_state(sym)
        params = with_overrides(load_params(self.strategy, st.inst.group), {"tf": tf, "htf": HTF_OF.get(tf, "4h")})
        strat = get_strategy(self.strategy, params)
        if self.service is not None and (sym, tf) in self.service.frames:
            return self.service.frames[(sym, tf)], strat, params
        frame = self._frame(st, tf)
        if frame is None or frame.is_empty():
            return None, strat, params
        return frame.tail(1).row(0, named=True), strat, params
