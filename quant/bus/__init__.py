"""Шина сообщений live-режима.

Прод: Redis Streams (consumer groups, MAXLEN) — QUANT_REDIS_URL задан.
Локально: шина в памяти процесса (все сервисы в одном процессе `quant live run --local`).

Потоки:
    bars.1m      — закрытые 1m-бары (last), {hh_symbol, ts, open..., source}
    mark.1m      — 1m mark-бары, {hh_symbol, ts, open, high, low, close}
    screener     — снапшоты скринера (pub/sub для API/WS)
    signals      — события сигналов (новый/смена статуса)
    accounts     — изменения счетов
    alerts       — служебные алерты
"""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from typing import Any, AsyncIterator

import orjson

log = logging.getLogger(__name__)


class Bus(ABC):
    @abstractmethod
    async def publish(self, stream: str, msg: dict[str, Any]) -> None: ...

    @abstractmethod
    def subscribe(self, stream: str, *, group: str | None = None, consumer: str = "c1") -> AsyncIterator[dict]: ...

    async def set_state(self, key: str, value: dict) -> None:  # последние значения (hash на символ)
        raise NotImplementedError

    async def get_state(self, key: str) -> dict | None:
        raise NotImplementedError

    async def close(self) -> None:
        pass


class MemoryBus(Bus):
    """Шина в памяти: fan-out на всех подписчиков; state — словарь."""

    def __init__(self, maxsize: int = 100_000):
        self._subs: dict[str, list[asyncio.Queue]] = defaultdict(list)
        self._state: dict[str, dict] = {}
        self._maxsize = maxsize

    async def publish(self, stream: str, msg: dict[str, Any]) -> None:
        for q in list(self._subs[stream]):
            if q.full():
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(msg)

    async def subscribe(self, stream: str, *, group: str | None = None, consumer: str = "c1") -> AsyncIterator[dict]:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._maxsize)
        self._subs[stream].append(q)
        try:
            while True:
                yield await q.get()
        finally:
            self._subs[stream].remove(q)

    async def set_state(self, key: str, value: dict) -> None:
        self._state[key] = value

    async def get_state(self, key: str) -> dict | None:
        return self._state.get(key)

    def state_keys(self, prefix: str) -> list[str]:
        return [k for k in self._state if k.startswith(prefix)]


class RedisStreamsBus(Bus):
    def __init__(self, url: str, maxlen: int = 200_000):
        import redis.asyncio as redis

        self.r = redis.from_url(url)
        self.maxlen = maxlen

    async def publish(self, stream: str, msg: dict[str, Any]) -> None:
        await self.r.xadd(stream, {"d": orjson.dumps(msg)}, maxlen=self.maxlen, approximate=True)

    async def subscribe(self, stream: str, *, group: str | None = None, consumer: str = "c1") -> AsyncIterator[dict]:
        if group:
            try:
                await self.r.xgroup_create(stream, group, id="$", mkstream=True)
            except Exception as e:  # BUSYGROUP — группа уже есть
                if "BUSYGROUP" not in str(e):
                    raise
            while True:
                resp = await self.r.xreadgroup(group, consumer, {stream: ">"}, count=500, block=5000)
                for _, entries in resp or []:
                    for eid, fields in entries:
                        yield orjson.loads(fields[b"d"])
                        await self.r.xack(stream, group, eid)
        else:
            last = "$"
            while True:
                resp = await self.r.xread({stream: last}, count=500, block=5000)
                for _, entries in resp or []:
                    for eid, fields in entries:
                        last = eid
                        yield orjson.loads(fields[b"d"])

    async def set_state(self, key: str, value: dict) -> None:
        await self.r.set(key, orjson.dumps(value))

    async def get_state(self, key: str) -> dict | None:
        v = await self.r.get(key)
        return orjson.loads(v) if v else None

    async def close(self) -> None:
        await self.r.aclose()


_BUS: Bus | None = None


def get_bus() -> Bus:
    global _BUS
    if _BUS is None:
        from quant.config import get_settings

        url = get_settings().redis_url
        _BUS = RedisStreamsBus(url) if url else MemoryBus()
        log.info("шина: %s", type(_BUS).__name__)
    return _BUS


def set_bus(bus: Bus) -> None:
    global _BUS
    _BUS = bus
