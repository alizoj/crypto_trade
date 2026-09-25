"""Флаги качества 1m-баров (3.2.2): zero_vol и halted.

«Замороженная» минута: volume == 0 и O=H=L=C (так Binance отдаёт минуты остановки/делистинга).
Серия ≥ halt_min_bars таких минут подряд (без пропусков по времени) — halted целиком.
Одиночные минуты с нулевым объёмом — только zero_vol.
"""

from __future__ import annotations

import polars as pl

HALT_MIN_BARS = 60
_MIN = pl.duration(minutes=1)


def frozen_expr() -> pl.Expr:
    return ((pl.col("volume") == 0) & (pl.col("open") == pl.col("high"))
            & (pl.col("high") == pl.col("low")) & (pl.col("low") == pl.col("close")))


def _runs(df: pl.DataFrame) -> pl.DataFrame:
    """Добавить _frozen, _run (номер серии) и _run_len. df отсортирован по ts."""
    return (
        df.with_columns(frozen_expr().fill_null(False).alias("_frozen"))
        .with_columns(
            ((pl.col("_frozen") != pl.col("_frozen").shift(1))
             | (pl.col("ts").diff() != _MIN)).fill_null(True).cum_sum().alias("_run")
        )
        .with_columns(pl.len().over("_run").alias("_run_len"))
    )


def add_flags(df: pl.DataFrame, halt_min_bars: int = HALT_MIN_BARS) -> pl.DataFrame:
    """Проставить zero_vol, halted (synthetic=False, если колонки нет). Ожидает дедуп и сортировку по ts."""
    if df.is_empty():
        out = df
        for c in ("zero_vol", "halted", "synthetic"):
            if c not in out.columns:
                out = out.with_columns(pl.lit(False).alias(c))
        return out
    r = _runs(df.sort("ts"))
    out = r.with_columns(
        (pl.col("volume") == 0).fill_null(False).alias("zero_vol"),
        (pl.col("_frozen") & (pl.col("_run_len") >= halt_min_bars)).alias("halted"),
    ).drop("_frozen", "_run", "_run_len")
    if "synthetic" not in out.columns:
        out = out.with_columns(pl.lit(False).alias("synthetic"))
    else:
        out = out.with_columns(pl.col("synthetic").fill_null(False))
    return out


def halted_runs(df: pl.DataFrame, halt_min_bars: int = HALT_MIN_BARS) -> pl.DataFrame:
    """Серии замороженных минут длиной ≥ halt_min_bars: start_ts, end_ts, minutes, price."""
    schema = {"start_ts": df.schema.get("ts", pl.Datetime("ms", "UTC")),
              "end_ts": df.schema.get("ts", pl.Datetime("ms", "UTC")),
              "minutes": pl.UInt32, "price": pl.Float64}
    if df.is_empty():
        return pl.DataFrame(schema=schema)
    r = _runs(df.sort("ts"))
    return (
        r.filter(pl.col("_frozen") & (pl.col("_run_len") >= halt_min_bars))
        .group_by("_run")
        .agg(pl.col("ts").min().alias("start_ts"), pl.col("ts").max().alias("end_ts"),
             pl.len().cast(pl.UInt32).alias("minutes"), pl.col("close").first().cast(pl.Float64).alias("price"))
        .sort("start_ts")
        .select(list(schema))
    )
