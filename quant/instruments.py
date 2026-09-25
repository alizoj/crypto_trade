"""Мастер-лист инструментов (config/instruments.csv, раздел 3.1 ТЗ).

Внутренний ключ везде — hh_symbol. Биржевые тикеры нужны только коллекторам и backfill.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Iterable, Literal

from quant.config import get_settings

AssetClass = Literal["crypto", "equity", "etf", "metal", "energy"]
Group = Literal["crypto_major", "crypto_alt", "tradfi"]
Source = Literal["binance_um", "bybit_linear", "binance_spot"]

# crypto_major = crypto с max_per_lot_usd >= порога (правило 3.1.7)
MAJOR_MIN_PER_LOT_USD = 100_000


def _parse_ts(value: str) -> datetime | None:
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _parse_float(value: str) -> float | None:
    return float(value) if value not in ("", None) else None


@dataclass(frozen=True)
class Instrument:
    hh_symbol: str
    base: str
    asset_class: AssetClass
    market: str
    underlying_type: str
    max_per_lot_usd: float
    max_total_usd: float
    primary_source: Source
    binance_um: str
    binance_um_mult: float
    bybit_linear: str | None
    bybit_linear_mult: float | None
    binance_spot: str | None
    binance_spot_mult: float | None
    binance_onboard: datetime | None
    bybit_launch: datetime | None
    history_start: datetime
    notes: str

    @property
    def group(self) -> Group:
        if self.asset_class != "crypto":
            return "tradfi"
        return "crypto_major" if self.max_per_lot_usd >= MAJOR_MIN_PER_LOT_USD else "crypto_alt"

    @property
    def is_tradfi(self) -> bool:
        return self.asset_class != "crypto"

    def source_symbol(self, source: str) -> str | None:
        return {
            "binance_um": self.binance_um,
            "bybit_linear": self.bybit_linear,
            "binance_spot": self.binance_spot,
        }[source]

    def multiplier(self, source: str) -> float:
        m = {
            "binance_um": self.binance_um_mult,
            "bybit_linear": self.bybit_linear_mult,
            "binance_spot": self.binance_spot_mult,
        }[source]
        if m is None:
            raise KeyError(f"{self.hh_symbol}: нет тикера на {source}")
        return m

    @property
    def history_start_ms(self) -> int:
        return int(self.history_start.timestamp() * 1000)


COLUMNS = [
    "hh_symbol", "base", "asset_class", "market", "underlying_type", "max_per_lot_usd",
    "max_total_usd", "primary_source", "binance_um", "binance_um_mult", "bybit_linear",
    "bybit_linear_mult", "binance_spot", "binance_spot_mult", "binance_onboard", "bybit_launch",
    "history_start", "notes",
]


def load_instruments(path: str | Path | None = None) -> list[Instrument]:
    p = Path(path) if path else get_settings().instruments_csv
    with open(p, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        missing = set(COLUMNS) - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"instruments.csv: нет колонок {sorted(missing)}")
        out: list[Instrument] = []
        for row in reader:
            out.append(Instrument(
                hh_symbol=row["hh_symbol"],
                base=row["base"],
                asset_class=row["asset_class"],  # type: ignore[arg-type]
                market=row["market"],
                underlying_type=row["underlying_type"],
                max_per_lot_usd=float(row["max_per_lot_usd"]),
                max_total_usd=float(row["max_total_usd"]),
                primary_source=row["primary_source"],  # type: ignore[arg-type]
                binance_um=row["binance_um"],
                binance_um_mult=float(row["binance_um_mult"]),
                bybit_linear=row["bybit_linear"] or None,
                bybit_linear_mult=_parse_float(row["bybit_linear_mult"]),
                binance_spot=row["binance_spot"] or None,
                binance_spot_mult=_parse_float(row["binance_spot_mult"]),
                binance_onboard=_parse_ts(row["binance_onboard"]),
                bybit_launch=_parse_ts(row["bybit_launch"]),
                history_start=_parse_ts(row["history_start"]),  # type: ignore[arg-type]
                notes=row["notes"],
            ))
    _check(out)
    return out


def _check(items: list[Instrument]) -> None:
    seen: set[str] = set()
    for it in items:
        if it.hh_symbol in seen:
            raise ValueError(f"instruments.csv: дубликат {it.hh_symbol}")
        seen.add(it.hh_symbol)
        if it.max_per_lot_usd > it.max_total_usd:
            raise ValueError(f"{it.hh_symbol}: max_per_lot_usd > max_total_usd")
        if it.history_start is None:
            raise ValueError(f"{it.hh_symbol}: пустой history_start")
        if it.bybit_linear and it.bybit_linear_mult is None:
            raise ValueError(f"{it.hh_symbol}: нет bybit_linear_mult")


@lru_cache(maxsize=4)
def _cached(path: str) -> tuple[Instrument, ...]:
    return tuple(load_instruments(path))


def instruments(path: str | Path | None = None) -> tuple[Instrument, ...]:
    return _cached(str(path or get_settings().instruments_csv))


def by_symbol(path: str | Path | None = None) -> dict[str, Instrument]:
    return {i.hh_symbol: i for i in instruments(path)}


def get(hh_symbol: str) -> Instrument:
    return by_symbol()[hh_symbol]


def select(symbols: Iterable[str] | None = None, *, groups: Iterable[str] | None = None,
           asset_classes: Iterable[str] | None = None) -> list[Instrument]:
    items = list(instruments())
    if symbols:
        wanted = {s.upper() for s in symbols}
        items = [i for i in items if i.hh_symbol in wanted or i.base in wanted]
    if groups:
        g = set(groups)
        items = [i for i in items if i.group in g]
    if asset_classes:
        a = set(asset_classes)
        items = [i for i in items if i.asset_class in a]
    return items
