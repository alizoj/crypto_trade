"""Набор сделок ступени A по многим инструментам + перевод в массивы ступени B."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import polars as pl

from quant.backtest.sim import SimArrays
from quant.research.barriers import FILLED, SymbolTrades, fees_r

GROUP_CODES = {"crypto_major": 0, "crypto_alt": 1, "tradfi": 2}
_PATH_KEYS = ["p_ts", "p_wm_lo", "p_wm_hi", "p_mc", "p_wl_lo", "p_wl_hi", "p_lc"]


@dataclass
class TradeSet:
    trades: pl.DataFrame                   # все сигналы (включая неисполненные), trade_id по порядку
    paths: dict[str, np.ndarray]
    funding: dict[str, np.ndarray]
    meta: dict = field(default_factory=dict)

    @property
    def filled(self) -> pl.DataFrame:
        return self.trades.filter(pl.col("status") == FILLED)

    @property
    def data_start_min(self) -> int:
        return int(self.meta.get("data_start_min", self.trades["entry_min"].min() or 0))

    @property
    def data_end_min(self) -> int:
        return int(self.meta.get("data_end_min", self.trades["exit_min"].max() or 0))

    def save(self, directory: Path) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.trades.write_parquet(directory / "trades.parquet")
        np.savez_compressed(directory / "paths.npz", **self.paths, **{f"fund_{k}": v for k, v in self.funding.items()})

    @classmethod
    def load(cls, directory: Path, meta: dict | None = None) -> "TradeSet":
        trades = pl.read_parquet(directory / "trades.parquet")
        z = np.load(directory / "paths.npz")
        paths = {k: z[k] for k in _PATH_KEYS}
        funding = {k[5:]: z[k] for k in z.files if k.startswith("fund_")}
        return cls(trades=trades, paths=paths, funding=funding, meta=meta or {})


def merge(parts: list[SymbolTrades], *, limits: dict[str, tuple[float, float, str]],
          maker: float, taker: float, meta: dict | None = None) -> TradeSet:
    """Склеить результаты по инструментам; смещения путей/funding пересчитываются.

    limits: {hh_symbol: (max_per_lot_usd, max_total_usd, group)}.
    """
    frames, paths, funds = [], {k: [] for k in _PATH_KEYS}, {"f_min": [], "f_amt": []}
    p_off = f_off = 0
    for sym_idx, part in enumerate(parts):
        t = part.trades
        if t.is_empty():
            continue
        lot, total, group = limits[part.hh_symbol]
        t = t.with_columns(
            (pl.col("p_start") + p_off).alias("p_start"),
            (pl.col("f_start") + f_off).alias("f_start"),
            pl.lit(sym_idx, dtype=pl.Int32).alias("sym_idx"),
            pl.lit(group).alias("group"),
            pl.lit(float(lot)).alias("max_lot"),
            pl.lit(float(total)).alias("max_total"),
        )
        frames.append(t)
        if part.paths is not None:
            for k in _PATH_KEYS:
                paths[k].append(part.paths[k])
            p_off += len(part.paths["p_ts"])
        if part.funding is not None:
            funds["f_min"].append(part.funding["f_min"])
            funds["f_amt"].append(part.funding["f_amt"])
            f_off += len(part.funding["f_min"])
    if not frames:
        raise ValueError("нет сделок: пустой набор сигналов")
    trades = pl.concat(frames, how="diagonal_relaxed").sort(["entry_min", "hh_symbol", "signal_ts"])
    trades = trades.with_row_index("trade_id")
    fr = fees_r(trades, maker, taker).fill_nan(None)
    trades = trades.with_columns(fr.alias("fees_r"))
    cat = {k: (np.concatenate(v) if v else np.zeros(0, dtype=np.int64 if k == "p_ts" else np.float64))
           for k, v in paths.items()}
    fcat = {"f_min": np.concatenate(funds["f_min"]) if funds["f_min"] else np.zeros(0, np.int64),
            "f_amt": np.concatenate(funds["f_amt"]) if funds["f_amt"] else np.zeros(0)}
    ts = TradeSet(trades=trades, paths=cat, funding=fcat, meta=dict(meta or {}))
    ts.trades = ts.trades.with_columns(_funding_r(ts).alias("funding_r"))
    ts.trades = ts.trades.with_columns(
        (pl.col("r_gross") - pl.col("fees_r").fill_null(0.0) + pl.col("funding_r").fill_null(0.0)).alias("r_net"))
    return ts


def _funding_r(ts: TradeSet) -> pl.Series:
    """Funding сделки в R (на 1 единицу, без учёта частичных выходов)."""
    t = ts.trades
    starts = t["f_start"].to_numpy()
    lens = t["f_len"].to_numpy()
    risk = t["risk_unit"].to_numpy()
    amt = ts.funding["f_amt"]
    out = np.zeros(t.height)
    for i in np.nonzero(lens)[0]:
        out[i] = amt[starts[i]:starts[i] + lens[i]].sum() / risk[i] if risk[i] > 0 else 0.0
    return pl.Series("funding_r", out)


def to_sim_arrays(ts: TradeSet, skip: np.ndarray | None = None) -> SimArrays:
    f = ts.filled.sort(["entry_min", "trade_id"])
    n = f.height

    def col(name: str, dtype=np.float64) -> np.ndarray:
        return np.ascontiguousarray(f[name].to_numpy().astype(dtype))

    return SimArrays(
        t_entry_min=col("entry_min", np.int64),
        t_side=col("side", np.int64),
        t_entry_px=col("entry_px"),
        t_entry_slip=col("entry_slip"),
        t_entry_liq=col("entry_liq", np.int64),
        t_sl=col("sl"),
        t_risk=col("risk_unit"),
        t_sym=col("sym_idx", np.int64),
        t_group=np.ascontiguousarray(f["group"].replace_strict(GROUP_CODES, return_dtype=pl.Int64).to_numpy()),
        t_max_lot=col("max_lot"),
        t_max_total=col("max_total"),
        t_skip=np.zeros(n, dtype=np.bool_) if skip is None else skip.astype(np.bool_),
        t_x1_min=col("x1_min", np.int64),
        t_x1_px=col("x1_px"),
        t_x1_slip=col("x1_slip"),
        t_x1_frac=col("x1_frac"),
        t_x2_min=col("x2_min", np.int64),
        t_x2_px=np.nan_to_num(col("x2_px")),
        t_x2_slip=col("x2_slip"),
        t_x2_frac=col("x2_frac"),
        t_pstart=col("p_start", np.int64),
        t_plen=col("p_len", np.int64),
        p_ts=np.ascontiguousarray(ts.paths["p_ts"].astype(np.int64)),
        p_wm_lo=np.ascontiguousarray(ts.paths["p_wm_lo"]),
        p_wm_hi=np.ascontiguousarray(ts.paths["p_wm_hi"]),
        p_mc=np.ascontiguousarray(ts.paths["p_mc"]),
        p_wl_lo=np.ascontiguousarray(ts.paths["p_wl_lo"]),
        p_wl_hi=np.ascontiguousarray(ts.paths["p_wl_hi"]),
        p_lc=np.ascontiguousarray(ts.paths["p_lc"]),
        t_fstart=col("f_start", np.int64),
        t_flen=col("f_len", np.int64),
        f_min=np.ascontiguousarray(ts.funding["f_min"].astype(np.int64)),
        f_amt=np.ascontiguousarray(ts.funding["f_amt"].astype(np.float64)),
    )


def _ranges(starts: np.ndarray, lens: np.ndarray) -> np.ndarray:
    if len(starts) == 0 or lens.sum() == 0:
        return np.zeros(0, np.int64)
    total = int(lens.sum())
    rep = np.repeat(starts - np.concatenate([[0], np.cumsum(lens)[:-1]]), lens)
    return (rep + np.arange(total, dtype=np.int64)).astype(np.int64)


def select(ts: TradeSet, mask: pl.Expr) -> TradeSet:
    """Подмножество сделок с уплотнением путей и funding (для окон walk-forward и сшивки OOS)."""
    sub = ts.trades.filter(mask)
    p_start = sub["p_start"].to_numpy().astype(np.int64)
    p_len = sub["p_len"].to_numpy().astype(np.int64)
    f_start = sub["f_start"].to_numpy().astype(np.int64)
    f_len = sub["f_len"].to_numpy().astype(np.int64)
    p_idx = _ranges(p_start, p_len)
    f_idx = _ranges(f_start, f_len)
    new_p = np.concatenate([[0], np.cumsum(p_len)[:-1]]).astype(np.int64) if len(p_len) else p_len
    new_f = np.concatenate([[0], np.cumsum(f_len)[:-1]]).astype(np.int64) if len(f_len) else f_len
    sub = sub.with_columns(pl.Series("p_start", new_p), pl.Series("f_start", new_f))
    paths = {k: v[p_idx] for k, v in ts.paths.items()}
    funding = {k: v[f_idx] for k, v in ts.funding.items()}
    return TradeSet(trades=sub, paths=paths, funding=funding, meta=dict(ts.meta))


def concat(parts: list[TradeSet], meta: dict | None = None) -> TradeSet:
    """Склеить наборы сделок (например, OOS-окна с разными параметрами)."""
    frames, p_off, f_off = [], 0, 0
    paths = {k: [] for k in _PATH_KEYS}
    funds = {"f_min": [], "f_amt": []}
    for ts in parts:
        frames.append(ts.trades.with_columns((pl.col("p_start") + p_off).alias("p_start"),
                                             (pl.col("f_start") + f_off).alias("f_start")))
        for k in _PATH_KEYS:
            paths[k].append(ts.paths[k])
        for k in funds:
            funds[k].append(ts.funding[k])
        p_off += len(ts.paths["p_ts"])
        f_off += len(ts.funding["f_min"])
    trades = pl.concat(frames, how="diagonal_relaxed").sort(["entry_min", "hh_symbol", "signal_ts"])
    trades = trades.drop("trade_id").with_row_index("trade_id")
    return TradeSet(trades=trades, paths={k: np.concatenate(v) for k, v in paths.items()},
                    funding={k: np.concatenate(v) for k, v in funds.items()}, meta=dict(meta or {}))
