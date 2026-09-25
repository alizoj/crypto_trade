"""Синтетика для санити-проверок модели (приёмка фазы 4).

- random_walk_bars: мартингал на 1m (лог-блуждание без сноса), OHLC из подшагов;
- fair_trades: независимые «честные» сделки (p=0.5, R:R 1:1) сразу в формате ступени B.
"""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import polars as pl

from quant.backtest.sim import SimArrays

T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def random_walk_bars(n_min: int, *, seed: int = 1, start: datetime = T0, price: float = 100.0,
                     sigma: float = 0.0005, substeps: int = 8) -> pl.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, sigma / np.sqrt(substeps), size=(n_min, substeps))
    path = np.exp(np.log(price) + np.cumsum(steps.ravel())).reshape(n_min, substeps)
    opens = np.concatenate([[price], path[:-1, -1]])
    highs = np.maximum(path.max(axis=1), opens)
    lows = np.minimum(path.min(axis=1), opens)
    closes = path[:, -1]
    ts = np.int64(start.timestamp() * 1000) + np.arange(n_min, dtype=np.int64) * 60_000
    df = pl.DataFrame({
        "ts": pl.Series(ts).cast(pl.Datetime("ms", "UTC")),
        "open": opens, "high": highs, "low": lows, "close": closes,
        "volume": np.ones(n_min), "quote_volume": closes, "trades": np.ones(n_min, dtype=np.int64),
        "taker_buy_volume": np.full(n_min, 0.5), "taker_buy_quote_volume": closes / 2,
        "halted": np.zeros(n_min, dtype=bool), "zero_vol": np.zeros(n_min, dtype=bool),
        "synthetic": np.zeros(n_min, dtype=bool), "version": np.full(n_min, 3, dtype=np.int8),
        "source": ["synthetic"] * n_min,
    })
    return df.with_columns(pl.col("open").alias("m_open"), pl.col("high").alias("m_high"),
                           pl.col("low").alias("m_low"), pl.col("close").alias("m_close"))


def fair_trades(n_challenges: int, per_challenge: int, *, spacing_min: int = 160, hold_min: int = 30,
                p_win: float = 0.5, rr: float = 1.0, seed: int = 0, start_min: int | None = None,
                ) -> tuple[SimArrays, np.ndarray, int]:
    """Независимые сегменты сделок: челлендж j торгует только в своём сегменте.

    Возвращает (массивы ступени B, старты сегментов, длина сегмента в минутах).
    Цена входа 100, SL 99 (риск 1), TP 100 + rr. Нулевые проскальзывания и funding.
    """
    rng = np.random.default_rng(seed)
    seg = per_challenge * spacing_min + 2 * 1440
    base = start_min if start_min is not None else int(T0.timestamp() // 60) + 20 * 60 + 10 - 24 * 60
    n = n_challenges * per_challenge
    j = np.repeat(np.arange(n_challenges), per_challenge)
    k = np.tile(np.arange(per_challenge), n_challenges)
    entry = base + j * seg + k * spacing_min + 1
    win = rng.random(n) < p_win
    side = np.where(rng.random(n) < 0.5, 1, -1)
    entry_px = np.full(n, 100.0)
    sl = entry_px - side * 1.0
    exit_px = np.where(win, entry_px + side * rr, entry_px - side * 1.0)
    exit_min = entry + hold_min
    # путь: 2 точки — минута входа и минута выхода (худшая = цена выхода)
    p_ts = np.empty(2 * n, np.int64)
    p_ts[0::2] = entry
    p_ts[1::2] = exit_min
    worst_entry = entry_px
    lo = np.empty(2 * n)
    hi = np.empty(2 * n)
    lo[0::2] = worst_entry
    hi[0::2] = worst_entry
    lo[1::2] = exit_px
    hi[1::2] = exit_px
    mc = lo.copy()
    starts = base + np.arange(n_challenges, dtype=np.int64) * seg
    arr = SimArrays(
        t_entry_min=entry.astype(np.int64), t_side=side.astype(np.int64), t_entry_px=entry_px,
        t_entry_slip=np.zeros(n), t_entry_liq=np.ones(n, np.int64), t_sl=sl, t_risk=np.ones(n),
        t_sym=np.zeros(n, np.int64), t_group=np.zeros(n, np.int64), t_max_lot=np.full(n, 1e12),
        t_max_total=np.full(n, 1e12), t_skip=np.zeros(n, np.bool_),
        t_x1_min=exit_min.astype(np.int64), t_x1_px=exit_px, t_x1_slip=np.zeros(n), t_x1_frac=np.ones(n),
        t_x2_min=np.full(n, -1, np.int64), t_x2_px=np.zeros(n), t_x2_slip=np.zeros(n), t_x2_frac=np.zeros(n),
        t_pstart=np.arange(0, 2 * n, 2, dtype=np.int64), t_plen=np.full(n, 2, np.int64),
        p_ts=p_ts, p_wm_lo=lo, p_wm_hi=hi, p_mc=mc, p_wl_lo=lo.copy(), p_wl_hi=hi.copy(), p_lc=mc.copy(),
        t_fstart=np.zeros(n, np.int64), t_flen=np.zeros(n, np.int64),
        f_min=np.zeros(0, np.int64), f_amt=np.zeros(0),
    )
    return arr, starts, seg


def fair_tradeset(n_days: int, *, p_win: float = 0.5, per_day: int = 6, seed: int = 0, start_min: int | None = None):
    """Один непрерывный ряд независимых сделок в формате TradeSet (для тестов оптимизатора/калибровки).

    score коррелирует с исходом только при p_win ≠ 0.5 через шум: score = 50 + 20·(win − 0.5) + N(0, 15).
    """
    from quant.backtest.tradeset import TradeSet

    spacing = 1440 // per_day
    arr, starts, _ = fair_trades(1, n_days * per_day, spacing_min=spacing, p_win=p_win, seed=seed, start_min=start_min)
    n = arr.n
    rng = np.random.default_rng(seed + 1)
    win = arr.t_x1_px * arr.t_side > arr.t_entry_px * arr.t_side
    score = np.clip(50 + 20 * (win.astype(float) - 0.5) + rng.normal(0, 15, n), 0, 100)
    r = np.where(win, 1.0, -1.0)
    ts_ms = arr.t_entry_min * 60_000
    trades = pl.DataFrame({
        "hh_symbol": ["SYN"] * n,
        "signal_ts": pl.Series(ts_ms).cast(pl.Datetime("ms", "UTC")),
        "tf": ["1h"] * n, "side": arr.t_side.astype(np.int8), "entry_type": ["market"] * n, "score": score,
        "status": np.zeros(n, np.int8), "delay_min": np.zeros(n, np.int64), "entry_min": arr.t_entry_min,
        "entry_px": arr.t_entry_px, "entry_slip": arr.t_entry_slip, "entry_liq": arr.t_entry_liq.astype(np.int8),
        "sl": arr.t_sl, "tp1": arr.t_entry_px + arr.t_side * 1.0, "tp2": np.full(n, np.nan), "tp1_frac": np.ones(n),
        "risk_unit": arr.t_risk, "x1_min": arr.t_x1_min, "x1_px": arr.t_x1_px, "x1_slip": arr.t_x1_slip,
        "x1_frac": arr.t_x1_frac, "x1_kind": np.where(win, 1, 3).astype(np.int8), "x2_min": arr.t_x2_min,
        "x2_px": np.full(n, np.nan), "x2_slip": arr.t_x2_slip, "x2_frac": arr.t_x2_frac,
        "x2_kind": np.zeros(n, np.int8), "exit_min": arr.t_x1_min, "exit_kind": np.where(win, 1, 3).astype(np.int8),
        "label": np.where(win, 1, 0).astype(np.int8), "ambiguous": np.zeros(n, bool), "r_gross": r,
        "mfe_r": np.where(win, 1.0, 0.0), "mae_r": np.where(win, 0.0, 1.0), "hold_min": arr.t_x1_min - arr.t_entry_min,
        "p_start": arr.t_pstart, "p_len": arr.t_plen, "f_start": arr.t_fstart, "f_len": arr.t_flen,
        "sym_idx": np.zeros(n, np.int32), "group": ["crypto_major"] * n, "max_lot": arr.t_max_lot,
        "max_total": arr.t_max_total, "fees_r": np.zeros(n), "funding_r": np.zeros(n), "r_net": r,
    }).with_row_index("trade_id")
    paths = {"p_ts": arr.p_ts, "p_wm_lo": arr.p_wm_lo, "p_wm_hi": arr.p_wm_hi, "p_mc": arr.p_mc,
             "p_wl_lo": arr.p_wl_lo, "p_wl_hi": arr.p_wl_hi, "p_lc": arr.p_lc}
    return TradeSet(trades=trades, paths=paths, funding={"f_min": arr.f_min, "f_amt": arr.f_amt},
                    meta={"data_start_min": int(starts[0]), "data_end_min": int(starts[0] + n_days * 1440)})
