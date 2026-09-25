"""Старшие ТФ — только агрегацией из дедуплицированных 1m (раздел 3, раздел 4).

Один и тот же код для бэктеста и live. Никаких materialized views.
"""

from __future__ import annotations

import polars as pl

from quant.timeutil import tf_minutes


def dedup_1m(bars: pl.DataFrame) -> pl.DataFrame:
    """Оставить по одному бару на ts: с максимальной version (архив > REST > WS)."""
    if bars.is_empty():
        return bars
    if "version" not in bars.columns:
        return bars.unique(subset=["ts"], keep="last").sort("ts")
    return bars.sort(["ts", "version"]).unique(subset=["ts"], keep="last", maintain_order=True).sort("ts")


def resample(bars_1m: pl.DataFrame, tf: str, *, offset_min: int = 0, complete_only: bool = True) -> pl.DataFrame:
    """1m → tf. Метка бара = время открытия; бар закрыт в ts + tf.

    offset_min — сдвиг сетки (например 20*60 для «проп-дня» с 20:00 UTC при tf=1d).
    complete_only — отбрасывать бары, у которых нет ни одной не-halted минуты.
    Колонки m_* (mark), если есть, агрегируются так же.
    Флаги: n_min — число минут в баре, halted — все минуты halted, has_synthetic.
    """
    n = tf_minutes(tf)
    if bars_1m.is_empty():
        return bars_1m
    if n == 1:
        out = bars_1m.with_columns(pl.lit(1, dtype=pl.Int32).alias("n_min"))
        return out
    every = f"{n}m"
    offset = f"{offset_min % n}m" if offset_min else "0m"
    aggs = [
        pl.col("open").first().alias("open"),
        pl.col("high").max().alias("high"),
        pl.col("low").min().alias("low"),
        pl.col("close").last().alias("close"),
        pl.len().cast(pl.Int32).alias("n_min"),
    ]
    for c in ("volume", "quote_volume", "taker_buy_volume", "taker_buy_quote_volume"):
        if c in bars_1m.columns:
            aggs.append(pl.col(c).sum().alias(c))
    if "trades" in bars_1m.columns:
        aggs.append(pl.col("trades").sum().alias("trades"))
    if "m_open" in bars_1m.columns:
        aggs += [
            pl.col("m_open").first().alias("m_open"),
            pl.col("m_high").max().alias("m_high"),
            pl.col("m_low").min().alias("m_low"),
            pl.col("m_close").last().alias("m_close"),
        ]
    if "halted" in bars_1m.columns:
        aggs.append(pl.col("halted").all().alias("halted"))
    if "synthetic" in bars_1m.columns:
        aggs.append(pl.col("synthetic").any().alias("has_synthetic"))
    src = bars_1m.sort("ts")
    if "halted" in src.columns and complete_only:
        # halted-минуты не формируют цену бара (3.2.2)
        live = src.filter(~pl.col("halted"))
    else:
        live = src
    out = (
        live.group_by_dynamic("ts", every=every, offset=offset, closed="left", label="left")
        .agg(aggs)
        .sort("ts")
    )
    if "halted" in out.columns:
        out = out.with_columns(pl.lit(False).alias("halted"))
    return out


def close_time(ts: pl.Expr, tf: str) -> pl.Expr:
    return ts + pl.duration(minutes=tf_minutes(tf))


def closed_bars(bars_tf: pl.DataFrame, tf: str, now_ms: int) -> pl.DataFrame:
    """Только закрытые к моменту now_ms бары (для live и защиты от look-ahead)."""
    n = tf_minutes(tf)
    return bars_tf.filter((pl.col("ts").dt.epoch("ms") + n * 60_000) <= now_ms)
