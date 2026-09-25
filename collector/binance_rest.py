"""Резервный коллектор Binance USDⓈ-M через REST-поллинг (когда WebSocket недоступен из сети).

Раз в минуту (через несколько секунд после закрытия) по каждому символу берутся последние закрытые
1m-свечи `/fapi/v1/klines` и mark-свечи `/fapi/v1/markPriceKlines`. Пропущенные минуты догружаются
тем же запросом (limit покрывает отставание). Бары публикуются в ту же шину, что и WS-коллектор.
Вес запросов: 172 символа × 2 × weight 1 ≈ 344/мин при лимите 2400/мин.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from collector.binance_ws import CollectorStats, _write_live
from quant import metrics as M
from quant.bus import Bus
from quant.instruments import Instrument

log = logging.getLogger(__name__)
BASE = "https://fapi.binance.com"
MAX_CATCHUP = 1500


class BinanceRestPoller:
    def __init__(self, instruments: list[Instrument], bus: Bus, *, write_store: bool = True, concurrency: int = 8,
                 delay_sec: float = 3.0, stats: CollectorStats | None = None):
        self.inst = list(instruments)
        self.bus = bus
        self.write_store = write_store
        self.sem = asyncio.Semaphore(concurrency)
        self.delay = delay_sec
        self.stats = stats or CollectorStats()
        self.last_sent: dict[str, int] = {}
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        async with httpx.AsyncClient(base_url=BASE, timeout=10) as client:
            while not self._stop.is_set():
                now = time.time()
                wait = 60 - (now % 60) + self.delay
                await asyncio.sleep(wait)
                t0 = time.time()
                await asyncio.gather(*(self._poll(client, i) for i in self.inst))
                log.debug("rest poll: %d символов за %.1f с", len(self.inst), time.time() - t0)

    async def _get(self, client: httpx.AsyncClient, path: str, params: dict) -> list:
        for attempt in range(3):
            try:
                async with self.sem:
                    r = await client.get(path, params=params)
                if r.status_code in (418, 429):
                    await asyncio.sleep(int(r.headers.get("Retry-After", "5")))
                    continue
                r.raise_for_status()
                return r.json()
            except Exception as e:
                if attempt == 2:
                    log.warning("rest %s %s: %s", path, params.get("symbol"), e)
                await asyncio.sleep(1 + attempt)
        return []

    def _last_stored(self, inst: Instrument) -> int | None:
        """Последний бар в хранилище — чтобы на старте догрузить разрыв до текущей минуты."""
        try:
            from datetime import timedelta

            from quant.data import store
            from quant.timeutil import now_utc

            df = store.load_bars(inst.hh_symbol, now_utc() - timedelta(days=2), with_mark=False)
            return int(df["ts"].dt.epoch("ms").max()) if df.height else None
        except Exception:
            return None

    async def _poll(self, client: httpx.AsyncClient, inst: Instrument) -> None:
        sym = inst.binance_um
        if inst.hh_symbol not in self.last_sent and self.write_store:
            stored = await asyncio.to_thread(self._last_stored, inst)
            if stored is not None:
                self.last_sent[inst.hh_symbol] = stored
        last = self.last_sent.get(inst.hh_symbol)
        now_ms = int(time.time() * 1000)
        # догрузка разрыва после простоя: до MAX_CATCHUP минут одним запросом (limit ≤ 1500 у Binance)
        missing = 2 if last is None else min(MAX_CATCHUP, max(2, (now_ms - last) // 60_000 + 1))
        kl, mk = await asyncio.gather(
            self._get(client, "/fapi/v1/klines", {"symbol": sym, "interval": "1m", "limit": missing}),
            self._get(client, "/fapi/v1/markPriceKlines", {"symbol": sym, "interval": "1m", "limit": missing}))
        mult = inst.binance_um_mult
        marks = {int(k[0]): k for k in mk}
        for k in kl:
            ts, close_time = int(k[0]), int(k[6])
            if close_time >= now_ms or (last is not None and ts <= last) or ts < inst.history_start_ms:
                continue
            bar = {"hh_symbol": inst.hh_symbol, "ts": ts, "open": float(k[1]) / mult, "high": float(k[2]) / mult,
                   "low": float(k[3]) / mult, "close": float(k[4]) / mult, "volume": float(k[5]) * mult,
                   "quote_volume": float(k[7]), "trades": int(k[8]), "taker_buy_volume": float(k[9]) * mult,
                   "taker_buy_quote_volume": float(k[10]), "source": "binance_um", "version": 2}
            m = marks.get(ts)
            if m is not None:
                await self.bus.publish("mark.1m", {"hh_symbol": inst.hh_symbol, "ts": ts, "open": float(m[1]) / mult,
                                                   "high": float(m[2]) / mult, "low": float(m[3]) / mult,
                                                   "close": float(m[4]) / mult})
                await self.bus.set_state(f"mark:{inst.hh_symbol}", {"p": float(m[4]) / mult, "ts": ts + 60_000})
            await self.bus.publish("bars.1m", bar)
            self.last_sent[inst.hh_symbol] = ts
            self.stats.bars += 1
            self.stats.last_bar_ts[inst.hh_symbol] = ts
            M.BARS.labels("binance_rest").inc()
            M.DATA_LAG.labels(inst.hh_symbol).set(max(0.0, time.time() - (ts + 60_000) / 1000))
            if self.write_store:
                await asyncio.to_thread(_write_live, inst.hh_symbol, bar)


async def run_auto(instruments: list[Instrument], bus: Bus, *, write_store: bool = True, probe_sec: int = 45,
                   stats: CollectorStats | None = None) -> None:
    """WS, а если за probe_sec не пришло ни одного сообщения — переключение на REST-поллинг."""
    from collector.binance_ws import BinanceCollector

    ws = BinanceCollector(instruments, bus, write_store=write_store)
    if stats is not None:
        ws.stats = stats
    task = asyncio.create_task(ws.run())
    t0 = time.time()
    while time.time() - t0 < probe_sec:
        await asyncio.sleep(1)
        if ws.stats.last_msg_at > 0:
            log.info("binance: WebSocket работает")
            await task
            return
    log.warning("binance: WebSocket не отдаёт данные %d с — переключаюсь на REST-поллинг", probe_sec)
    ws.stop()
    task.cancel()
    await BinanceRestPoller(instruments, bus, write_store=write_store, stats=ws.stats).run()
