"""Широта рынка: доля инструментов, у которых на закрытии бара выполнены правила над признаками бара.

Считается по всем инструментам вселенной (не только по торгуемым) на тех же признаках, что видит стратегия
(quant.research.features), только по закрытым барам. Знаменатель — инструменты с готовым ATR на этом баре.
Результат — кадр close_ts, n_sym, n_long, n_short, frac_long, frac_short (по возрастанию close_ts); стратегия
feature_rules получает его в meta["breadth"] и берёт сигнал, только если доля >= breadth_min_frac.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta

import numpy as np
import polars as pl

from quant import instruments as I
from quant.data import store
from quant.data.aggregate import resample

log = logging.getLogger(__name__)
_CACHE: dict[str, pl.DataFrame] = {}

LoadBars = Callable[[I.Instrument], pl.DataFrame]


def breadth_series(insts: list[I.Instrument], start: datetime, end: datetime, *, tf: str, htf_tf: str,
                   rules_long: list, rules_short: list, load_bars: LoadBars, warmup_days: int,
                   btc: pl.DataFrame | None = None) -> pl.DataFrame:
    """Доля инструментов с выполненными правилами на каждом закрытии бара ТФ в [start, end)."""
    from quant.research.features import bar_features, rules_mask

    lo_ms, hi_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    parts = []
    for inst in insts:
        bars = load_bars(inst)
        if bars is None or bars.is_empty():
            continue
        meta = {"instrument": inst,
                "funding": store.load_funding(inst.hh_symbol, start - timedelta(days=warmup_days), end),
                "metrics": store.load_metrics(inst.hh_symbol, start - timedelta(days=warmup_days), end)}
        if btc is not None and inst.group != "crypto_major":
            meta["btc_trend"] = btc
        _, _, f = bar_features(resample(bars, tf), tf=tf, htf_tf=htf_tf, bars_1m=bars, meta=meta)
        if not f:
            continue
        n = len(f["_close"])
        with np.errstate(invalid="ignore"):
            ready = ~np.isnan(f["_atr"]) & (f["_atr"] > 0)
        cm = f["_close_ms"].astype(np.int64)
        keep = ready & (cm >= lo_ms) & (cm < hi_ms)
        if not keep.any():
            continue
        parts.append(pl.DataFrame({"close_ms": cm[keep],
                                   "hit_long": rules_mask(f, rules_long, n)[keep],
                                   "hit_short": rules_mask(f, rules_short, n)[keep]}))
    if not parts:
        return pl.DataFrame(schema={"close_ts": pl.Datetime("ms", "UTC"), "n_sym": pl.Int64, "n_long": pl.Int64,
                                    "n_short": pl.Int64, "frac_long": pl.Float64, "frac_short": pl.Float64})
    return (pl.concat(parts).group_by("close_ms")
            .agg(pl.len().cast(pl.Int64).alias("n_sym"), pl.col("hit_long").sum().cast(pl.Int64).alias("n_long"),
                 pl.col("hit_short").sum().cast(pl.Int64).alias("n_short"))
            .with_columns((pl.col("n_long") / pl.col("n_sym")).alias("frac_long"),
                          (pl.col("n_short") / pl.col("n_sym")).alias("frac_short"),
                          pl.from_epoch("close_ms", time_unit="ms").dt.replace_time_zone("UTC").alias("close_ts"))
            .sort("close_ms").select("close_ts", "n_sym", "n_long", "n_short", "frac_long", "frac_short"))


def cached_breadth(start: datetime, end: datetime, *, tf: str, htf_tf: str, rules_long: list, rules_short: list,
                   load_bars: LoadBars, warmup_days: int, btc: pl.DataFrame | None = None,
                   universe: list[I.Instrument] | None = None) -> pl.DataFrame:
    """breadth_series по всей вселенной instruments.csv с кэшем на процесс (walk-forward/скрининг зовут много раз)."""
    insts = universe if universe is not None else list(I.instruments())
    key = json.dumps([start.isoformat(), end.isoformat(), tf, htf_tf, rules_long, rules_short,
                      sorted(i.hh_symbol for i in insts)], default=str)
    if key not in _CACHE:
        log.info("широта рынка: %d инструментов, %s .. %s", len(insts), start, end)
        _CACHE[key] = breadth_series(insts, start, end, tf=tf, htf_tf=htf_tf, rules_long=rules_long,
                                     rules_short=rules_short, load_bars=load_bars, warmup_days=warmup_days, btc=btc)
    return _CACHE[key]
