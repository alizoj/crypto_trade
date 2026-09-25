"""Резервный коллектор Bybit linear (правило 3.1.8): включается по символу, когда поток Binance
молчит дольше порога. Бары помечаются source=bybit_linear и в хранилище не пишутся (один источник
на ряд — раздел 3.1.8); сигналы по ним помечаются источником.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time

import orjson
import websockets

from collector.binance_ws import CollectorStats
from quant import metrics as M
from quant.bus import Bus
from quant.instruments import Instrument

log = logging.getLogger(__name__)
WS_URL = "wss://stream.bybit.com/v5/public/linear"
MAX_ARGS = 10          # Bybit: не более 10 args в одном subscribe


class BybitBackup:
    def __init__(self, instruments: list[Instrument], bus: Bus, primary: CollectorStats, *, stale_sec: int = 180):
        self.inst = {i.bybit_linear: i for i in instruments if i.bybit_linear}
        self.bus = bus
        self.primary = primary
        self.stale_sec = stale_sec
        self.stats = CollectorStats()
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    def _primary_stale(self, hh_symbol: str, bar_ts_ms: int) -> bool:
        last = self.primary.last_bar_ts.get(hh_symbol)
        return last is None or bar_ts_ms - last > self.stale_sec * 1000

    async def run(self) -> None:
        topics = [f"kline.1.{s}" for s in self.inst]
        backoff = 1.0
        while not self._stop.is_set():
            try:
                async with websockets.connect(WS_URL, ping_interval=None, open_timeout=20) as ws:
                    for i in range(0, len(topics), MAX_ARGS):
                        await ws.send(orjson.dumps({"op": "subscribe", "args": topics[i:i + MAX_ARGS]}).decode())
                    backoff = 1.0
                    pinger = asyncio.create_task(self._ping(ws))
                    try:
                        async for raw in ws:
                            self.stats.last_msg_at = time.time()
                            await self._on_message(orjson.loads(raw))
                    finally:
                        pinger.cancel()
            except Exception as e:
                self.stats.reconnects += 1
                M.RECONNECTS.labels("bybit_linear").inc()
                log.warning("bybit ws: %s — переподключение через %.0f с", e, backoff)
                await asyncio.sleep(backoff + random.random())
                backoff = min(backoff * 2, 60)

    async def _ping(self, ws) -> None:  # noqa: ANN001
        while True:
            await asyncio.sleep(20)
            await ws.send('{"op":"ping"}')

    async def _on_message(self, msg: dict) -> None:
        topic = msg.get("topic", "")
        if not topic.startswith("kline.1."):
            return
        sym = topic.split(".", 2)[2]
        inst = self.inst.get(sym)
        if inst is None:
            return
        for k in msg.get("data", []):
            if not k.get("confirm"):
                continue
            ts = int(k["start"])
            if not self._primary_stale(inst.hh_symbol, ts):
                continue
            mult = inst.bybit_linear_mult or 1.0
            bar = {"hh_symbol": inst.hh_symbol, "ts": ts, "open": float(k["open"]) / mult,
                   "high": float(k["high"]) / mult, "low": float(k["low"]) / mult, "close": float(k["close"]) / mult,
                   "volume": float(k["volume"]) * mult, "quote_volume": float(k["turnover"]), "trades": 0,
                   "taker_buy_volume": None, "taker_buy_quote_volume": None, "source": "bybit_linear", "version": 1}
            self.stats.bars += 1
            M.BARS.labels("bybit_linear").inc()
            await self.bus.publish("bars.1m", bar)
