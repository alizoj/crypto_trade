"""Классические метрики (раздел 7, вторичные) по сделкам с фиксированным риском 1R."""

from __future__ import annotations

import math

import numpy as np
import polars as pl

from quant.research.barriers import FILLED
from quant.timeutil import prop_day_id


def daily_r(trades: pl.DataFrame, boundary_min: int = 1200) -> pl.DataFrame:
    """R по проп-дням выхода (для Sharpe/Sortino/DSR/PBO)."""
    f = trades.filter(pl.col("status") == FILLED)
    if f.is_empty():
        return pl.DataFrame({"day": [], "r": []}, schema={"day": pl.Int64, "r": pl.Float64})
    off = 1440 - boundary_min
    return (f.with_columns(((pl.col("exit_min") + off) // 1440).alias("day"))
             .group_by("day").agg(pl.col("r_net").sum().alias("r")).sort("day"))


def classic_metrics(trades: pl.DataFrame, boundary_min: int = 1200) -> dict:
    f = trades.filter(pl.col("status") == FILLED)
    n_sig = trades.height
    m: dict = {"n_signals": n_sig, "n_trades": f.height}
    if n_sig:
        m["fill_rate"] = f.height / n_sig
    if f.is_empty():
        return m
    r = f["r_net"].to_numpy()
    gross = f["r_gross"].to_numpy()
    wins = r[r > 0].sum()
    losses = -r[r < 0].sum()
    m["winrate"] = float((r > 0).mean())
    m["avg_r"] = float(r.mean())
    m["median_r"] = float(np.median(r))
    m["pf"] = float(wins / losses) if losses > 0 else float("inf")
    m["total_r"] = float(r.sum())
    order = np.argsort(f["exit_min"].to_numpy(), kind="stable")
    cum = np.cumsum(r[order])
    peak = np.maximum.accumulate(np.concatenate([[0.0], cum]))[1:]
    m["max_dd_r"] = float((peak - cum).max()) if len(cum) else 0.0
    fees = f["fees_r"].fill_null(0.0).to_numpy()
    m["fee_share"] = float(fees.sum() / np.abs(gross).sum()) if np.abs(gross).sum() > 0 else 0.0
    m["avg_fees_r"] = float(fees.mean())
    m["avg_hold_min"] = float(f["hold_min"].mean())
    lab = f["label"].to_numpy()
    m["p_tp"] = float((lab == 1).mean())
    m["p_sl"] = float((lab == 0).mean())
    m["p_timeout"] = float((lab == 2).mean())
    m["ambiguous_share"] = float(f["ambiguous"].mean())
    m["mfe_r_median"] = float(np.nanmedian(f["mfe_r"].to_numpy()))
    m["mae_r_median"] = float(np.nanmedian(f["mae_r"].to_numpy()))
    d = daily_r(trades, boundary_min)["r"].to_numpy()
    if len(d) > 2:
        sd = d.std(ddof=1)
        m["sharpe_daily"] = float(d.mean() / sd) if sd > 0 else 0.0
        m["sharpe_ann"] = m["sharpe_daily"] * math.sqrt(365)
        dn = d[d < 0]
        dsd = math.sqrt((dn ** 2).sum() / len(d)) if len(dn) else 0.0
        m["sortino_ann"] = float(d.mean() / dsd * math.sqrt(365)) if dsd > 0 else float("inf")
        m["trading_days"] = int(len(d))
    for side, name in ((1, "long"), (-1, "short")):
        s = f.filter(pl.col("side") == side)
        if s.height:
            m[f"n_{name}"] = s.height
            m[f"winrate_{name}"] = float((s["r_net"] > 0).mean())
            m[f"avg_r_{name}"] = float(s["r_net"].mean())
    return m


def by_group(trades: pl.DataFrame) -> pl.DataFrame:
    f = trades.filter(pl.col("status") == FILLED)
    if f.is_empty():
        return pl.DataFrame()
    return (f.group_by(["group", "side"]).agg(
        pl.len().alias("n"), (pl.col("r_net") > 0).mean().alias("winrate"), pl.col("r_net").mean().alias("avg_r"),
        (pl.col("label") == 1).mean().alias("p_tp"), pl.col("ambiguous").mean().alias("ambiguous"))
        .sort(["group", "side"]))
