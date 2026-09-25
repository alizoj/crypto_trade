"""Коллектор Binance USDⓈ-M (раздел 4): WS с reconnect/heartbeat → шина + live-бары в хранилище.

Потоки на символ: `<sym>@kline_1m` (закрытые бары, x=true) и `<sym>@markPrice@1s` (из секундных
mark-цен собираются 1m mark-бары). Время — биржевое (поля событий), не локальные часы.
Бары пишутся в хранилище как предварительные (version=1) и на T+1 заменяются архивом (раздел 3.2.5).
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from dataclasses import dataclass, field

import orjson
import websockets

from quant import metrics as M
from quant.bus import Bus
from quant.instruments import Instrument

log = logging.getLogger(__name__)
WS_URL = "wss://fstream.binance.com/stream?streams="
MAX_STREAMS_PER_CONN = 180


@dataclass
class MarkAgg:
    minute: int = -1
    o: float = 0.0
    h: float = 0.0
    l: float = 0.0
    c: float = 0.0


@dataclass
class CollectorStats:
    connected: int = 0
    reconnects: int = 0
    bars: int = 0
    last_bar_ts: dict[str, int] = field(default_factory=dict)     # hh_symbol → ms времени открытия
    last_msg_at: float = 0.0


class BinanceCollector:
    def __init__(self, instruments: list[Instrument], bus: Bus, *, write_store: bool = True):
        self.inst = {i.binance_um: i for i in instruments}
        self.bus = bus
        self.write_store = write_store
        self.marks: dict[str, MarkAgg] = {}
        self.stats = CollectorStats()
        self._stop = asyncio.Event()

    def _streams(self) -> list[list[str]]:
        names = []
        for sym in self.inst:
            s = sym.lower()
            names += [f"{s}@kline_1m", f"{s}@markPrice@1s"]
        return [names[i:i + MAX_STREAMS_PER_CONN] for i in range(0, len(names), MAX_STREAMS_PER_CONN)]

    async def run(self) -> None:
        await asyncio.gather(*(self._conn_loop(chunk, k) for k, chunk in enumerate(self._streams())))

    def stop(self) -> None:
        self._stop.set()

    async def _conn_loop(self, streams: list[str], k: int) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(WS_URL + "/".join(streams), ping_interval=20, ping_timeout=20,
                                              max_size=2 ** 22, open_timeout=20) as ws:
                    self.stats.connected += 1
                    log.info("binance ws #%d: подключено (%d потоков)", k, len(streams))
                    backoff = 1.0
                    async for raw in ws:
                        self.stats.last_msg_at = time.time()
                        await self._on_message(orjson.loads(raw))
                        if self._stop.is_set():
                            break
            except Exception as e:  # сеть/протокол — переподключение с backoff
                self.stats.reconnects += 1
                M.RECONNECTS.labels("binance_um").inc()
                log.warning("binance ws #%d: %s — переподключение через %.0f с", k, e, backoff)
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, 60)
            finally:
                self.stats.connected = max(0, self.stats.connected - 1)

    async def _on_message(self, msg: dict) -> None:
        data = msg.get("data") or {}
        ev = data.get("e")
        if ev == "kline":
            k = data["k"]
            if not k.get("x"):
                return
            await self._on_closed_kline(data["s"], k)
        elif ev == "markPriceUpdate":
            await self._on_mark(data["s"], int(data["E"]), float(data["p"]))

    async def _on_closed_kline(self, sym: str, k: dict) -> None:
        inst = self.inst.get(sym)
        if inst is None:
            return
        mult = inst.binance_um_mult
        ts = int(k["t"])
        if ts < inst.history_start_ms:
            return
        bar = {
            "hh_symbol": inst.hh_symbol, "ts": ts,
            "open": float(k["o"]) / mult, "high": float(k["h"]) / mult, "low": float(k["l"]) / mult,
            "close": float(k["c"]) / mult, "volume": float(k["v"]) * mult, "quote_volume": float(k["q"]),
            "trades": int(k["n"]), "taker_buy_volume": float(k["V"]) * mult, "taker_buy_quote_volume": float(k["Q"]),
            "source": "binance_um", "version": 1,
        }
        self.stats.bars += 1
        self.stats.last_bar_ts[inst.hh_symbol] = ts
        M.BARS.labels("binance_um").inc()
        M.DATA_LAG.labels(inst.hh_symbol).set(max(0.0, time.time() - (ts + 60_000) / 1000))
        await self.bus.publish("bars.1m", bar)
        if self.write_store:
            await asyncio.to_thread(_write_live, inst.hh_symbol, bar)

    async def _on_mark(self, sym: str, event_ms: int, price: float) -> None:
        inst = self.inst.get(sym)
        if inst is None:
            return
        price /= inst.binance_um_mult
        minute = event_ms // 60_000
        agg = self.marks.setdefault(sym, MarkAgg())
        if agg.minute != minute:
            if agg.minute >= 0:
                await self.bus.publish("mark.1m", {"hh_symbol": inst.hh_symbol, "ts": agg.minute * 60_000,
                                                   "open": agg.o, "high": agg.h, "low": agg.l, "close": agg.c})
            agg.minute, agg.o, agg.h, agg.l, agg.c = minute, price, price, price, price
        else:
            agg.h = max(agg.h, price)
            agg.l = min(agg.l, price)
            agg.c = price
        await self.bus.set_state(f"mark:{inst.hh_symbol}", {"p": price, "ts": event_ms})


def _write_live(hh_symbol: str, bar: dict) -> None:
    import polars as pl

    from quant.data import store
    from quant.schemas import BARS_1M, conform

    df = pl.DataFrame([{**bar, "halted": False, "zero_vol": bar["volume"] == 0, "synthetic": False}])
    df = df.with_columns(pl.col("ts").cast(pl.Datetime("ms", "UTC")))
    try:
        store.write_live_bars(hh_symbol, conform(df.drop("hh_symbol"), BARS_1M, extra=False))
    except Exception:  # хранилище не должно ронять коллектор
        log.exception("не удалось записать live-бар %s", hh_symbol)
