"""Контракты данных между модулями (Polars-схемы).

Все цены/объёмы в нормализованном слое — за 1 единицу базового актива (правило 3.1.2).
Время — Datetime("ms", "UTC"); ts свечи = время открытия.
"""

from __future__ import annotations

import polars as pl

TS = pl.Datetime("ms", "UTC")

# Нормализованные 1m-бары (last price) основного источника + флаги качества (раздел 3.2).
BARS_1M = {
    "ts": TS,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
    "volume": pl.Float64,                 # в базовом активе (нормализовано)
    "quote_volume": pl.Float64,           # в USDT
    "trades": pl.Int64,
    "taker_buy_volume": pl.Float64,       # → CVD: delta = 2*taker_buy_volume - volume
    "taker_buy_quote_volume": pl.Float64,
    "halted": pl.Boolean,                 # серия «замороженных» минут (3.2.2)
    "zero_vol": pl.Boolean,
    "synthetic": pl.Boolean,              # заполненный пропуск — не для исполнения
    "version": pl.Int8,                   # 3 архив > 2 REST > 1 WS (3.2.5)
    "source": pl.Utf8,
}

# Mark price 1m (нормализовано). В load_bars(with_mark=True) приклеивается как m_open..m_close.
MARK_1M = {
    "ts": TS,
    "open": pl.Float64,
    "high": pl.Float64,
    "low": pl.Float64,
    "close": pl.Float64,
}

FUNDING = {
    "ts": TS,                             # fundingTime, округлено до секунды
    "rate": pl.Float64,
    "mark_price": pl.Float64,             # нормализовано; null, если нет в источнике
    "source": pl.Utf8,
}

# Сигналы стратегии (вход движка бэктеста и live-сервиса).
SIGNALS = {
    "ts": TS,                             # момент, когда сигнал известен = закрытие бара сигнала
    "hh_symbol": pl.Utf8,
    "tf": pl.Utf8,
    "side": pl.Int8,                      # +1 long, -1 short
    "entry_type": pl.Utf8,                # market | limit
    "entry_price": pl.Float64,            # для market — close бара сигнала (ориентир)
    "sl": pl.Float64,
    "tp1": pl.Float64,
    "tp2": pl.Float64,                    # null, если один тейк
    "tp1_frac": pl.Float64,               # доля позиции на TP1 (1.0 — один тейк)
    "score": pl.Float64,                  # 0..100
    "max_hold_min": pl.Int32,             # таймаут сделки в минутах
    "valid_min": pl.Int32,                # срок жизни лимитного входа в минутах
}

SIGNAL_COMPONENT_PREFIX = "c_"            # c_<фактор> — вклад фактора в баллы


def empty(schema: dict) -> pl.DataFrame:
    return pl.DataFrame(schema=schema)


def conform(df: pl.DataFrame, schema: dict, *, extra: bool = True) -> pl.DataFrame:
    """Привести колонки к схеме (типы), недостающие — null. extra=True сохраняет лишние колонки."""
    if df.width == 0 or df.height == 0 and not set(schema) & set(df.columns):
        # литералы на пустом кадре дали бы одну строку из null — возвращаем пустой кадр со схемой
        return pl.DataFrame(schema=schema)
    cols = []
    for name, dtype in schema.items():
        if name in df.columns:
            cols.append(pl.col(name).cast(dtype))
        else:
            cols.append(pl.lit(None, dtype=dtype).alias(name))
    rest = [c for c in df.columns if c not in schema] if extra else []
    return df.select(cols + rest)
