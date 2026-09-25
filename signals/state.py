"""Состояние рынка для live: буферы 1m, бары ТФ и HTF по каждому символу.

Прогрев — из хранилища (тот же load_bars, что в бэктесте), дальше бары ТФ собираются из
закрытых 1m тем же resample — поэтому live-сигналы совпадают с бэктестом на тех же барах.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import polars as pl

from quant.data import store
from quant.data.aggregate import dedup_1m, resample
from quant.instruments import Instrument
from quant.schemas import BARS_1M, conform
from quant.timeutil import MS_PER_MIN, from_ms, now_utc, tf_minutes

log = logging.getLogger(__name__)

SCREEN_TFS = ("15m", "1h", "4h")
HTF_OF = {"15m": "1h", "1h": "4h", "4h": "1d"}
KEEP_TF_BARS = 800            # окно ТФ для compute (≥ warmup стратегии 700)
KEEP_HTF_BARS = 2600          # EMA200 на HTF: (1 − 2/201)^2400 ≈ 3e-10
KEEP_1M_DAYS = 2
WARMUP_DAYS = 440             # 4h × 2600 ≈ 433 дня


@dataclass
class SymbolState:
    inst: Instrument
    m1: pl.DataFrame
    bars: dict[str, pl.DataFrame] = field(default_factory=dict)      # tf → бары (включая HTF-ТФ)
    last_bar_ms: int | None = None
    last_mark: float | None = None
    source: str = "binance_um"
    funding: pl.DataFrame | None = None
    metrics: pl.DataFrame | None = None

    @property
    def price(self) -> float | None:
        return float(self.m1["close"][-1]) if self.m1.height else None

    @property
    def halted(self) -> bool:
        return bool(self.m1.height and self.m1["halted"].tail(1).fill_null(False)[0])

    def change_24h_pct(self) -> float | None:
        if self.m1.height < 2:
            return None
        last_ts = self.m1["ts"][-1]
        prev = self.m1.filter(pl.col("ts") <= last_ts - timedelta(hours=24)).tail(1)
        if prev.is_empty():
            return None
        p0 = float(prev["close"][0])
        return (self.price / p0 - 1) * 100 if p0 else None


def all_tfs() -> list[str]:
    return sorted(set(SCREEN_TFS) | set(HTF_OF.values()), key=tf_minutes)


def warmup(inst: Instrument, *, now: datetime | None = None, days: int = WARMUP_DAYS) -> SymbolState:
    now = now or now_utc()
    bars = store.load_bars(inst.hh_symbol, now - timedelta(days=days), now, with_mark=True)
    st = SymbolState(inst=inst, m1=bars.filter(pl.col("ts") >= now - timedelta(days=KEEP_1M_DAYS)))
    for tf in all_tfs():
        agg = resample(bars, tf) if bars.height else bars
        keep = KEEP_HTF_BARS if tf in HTF_OF.values() else KEEP_TF_BARS
        st.bars[tf] = _closed(agg, tf, now).tail(keep)
    if bars.height:
        st.last_bar_ms = int(bars["ts"].dt.epoch("ms").max())
    st.funding = store.load_funding(inst.hh_symbol, now - timedelta(days=30), now)
    st.metrics = store.load_metrics(inst.hh_symbol, now - timedelta(days=3), now)
    return st


def _closed(df: pl.DataFrame, tf: str, now: datetime) -> pl.DataFrame:
    if df.is_empty():
        return df
    n = tf_minutes(tf) * MS_PER_MIN
    return df.filter((pl.col("ts").dt.epoch("ms") + n) <= int(now.timestamp() * 1000))


def on_bar(st: SymbolState, bar: dict) -> list[str]:
    """Добавить закрытый 1m-бар. Возвращает список ТФ, чьи бары закрылись этой минутой."""
    row = {k: bar.get(k) for k in BARS_1M}
    row["ts"] = from_ms(int(bar["ts"]))
    row.setdefault("halted", False)
    row["halted"] = bool(row.get("halted") or False)
    row["zero_vol"] = (bar.get("volume") or 0) == 0
    row["synthetic"] = False
    row["version"] = int(bar.get("version") or 1)
    df = conform(pl.DataFrame([row]), BARS_1M, extra=False)
    for c in ("m_open", "m_high", "m_low", "m_close"):
        df = df.with_columns(pl.lit(None, dtype=pl.Float64).alias(c))
    if st.m1.height:
        df = df.select(st.m1.columns) if set(st.m1.columns) <= set(df.columns) else df
    st.m1 = dedup_1m(pl.concat([st.m1, df], how="diagonal_relaxed"))
    cutoff = row["ts"] - timedelta(days=KEEP_1M_DAYS)
    st.m1 = st.m1.filter(pl.col("ts") >= cutoff)
    st.last_bar_ms = int(bar["ts"])
    st.source = bar.get("source", "binance_um")
    closed = []
    end_ms = int(bar["ts"]) + MS_PER_MIN
    for tf in all_tfs():
        n = tf_minutes(tf) * MS_PER_MIN
        if end_ms % n:
            continue
        start = from_ms(end_ms - n)
        chunk = st.m1.filter(pl.col("ts") >= start)
        if chunk.is_empty():
            continue
        agg = resample(chunk, tf)
        agg = agg.filter(pl.col("ts") == start)
        if agg.is_empty():
            continue
        prev = st.bars.get(tf)
        merged = agg if prev is None or prev.is_empty() else pl.concat([prev, agg], how="diagonal_relaxed")
        keep = KEEP_HTF_BARS if tf in HTF_OF.values() else KEEP_TF_BARS
        st.bars[tf] = merged.unique(subset=["ts"], keep="last", maintain_order=True).sort("ts").tail(keep)
        closed.append(tf)
    return closed


def on_mark(st: SymbolState, mark: dict) -> None:
    """1m mark-бар: приклеить m_* к соответствующему 1m-бару и запомнить последнюю mark."""
    st.last_mark = float(mark["close"])
    ts = from_ms(int(mark["ts"]))
    if st.m1.height and "m_open" in st.m1.columns:
        st.m1 = st.m1.with_columns(
            *[pl.when(pl.col("ts") == ts).then(pl.lit(float(mark[k]))).otherwise(pl.col(f"m_{k}")).alias(f"m_{k}")
              for k in ("open", "high", "low", "close")])
