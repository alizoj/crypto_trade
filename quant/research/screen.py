"""Уровень 1 исследования (раздел 7): быстрый векторный скрининг вариантов стратегии.

Для каждого варианта параметров — сигналы и метки тройного барьера с MFE/MAE (без путей и без
симуляции счёта), классические метрики в R. Каждый вариант пишется в реестр испытаний.
Лучшие варианты затем проверяются событийным бэктестом с propsim (уровень 2).
"""

from __future__ import annotations

import logging
from datetime import datetime

import numpy as np
import polars as pl

from quant.backtest.engine import BoundedBarsCache, BuildSpec, build_tradeset
from quant.backtest.metrics import classic_metrics
from quant.data import store
from quant.optimize import registry
from quant.optimize.walkforward import grid

log = logging.getLogger(__name__)


def screen(strategy: str, symbols: list[str], start: datetime, end: datetime, *, search_space: dict[str, list],
           study: str, max_combos: int | None = 64, min_trades: int = 30, bars_cache_gb: float = 4) -> pl.DataFrame:
    dhash = store.dataset_hash(symbols, start, end)
    combos = grid({}, search_space, max_combos)
    cache = BoundedBarsCache(bars_cache_gb * 1e9)
    rows = []
    for i, c in enumerate(combos):
        overrides = dict(c["point"])
        try:
            ts = build_tradeset(BuildSpec(strategy=strategy, symbols=symbols, start=start, end=end,
                                          overrides=overrides, record_path=False, use_book_depth=False),
                                bars_cache=cache)
        except ValueError:
            continue
        m = classic_metrics(ts.trades)
        n = m.get("n_trades", 0)
        ev_r = m.get("avg_r")
        registry.record_trial(study=study, level="research", strategy=strategy, params=overrides,
                              dataset_hash=dhash, metrics=m)
        rows.append({"combo": i, **{k: str(v) for k, v in overrides.items()}, "n_trades": n,
                     "short_share": m.get("n_short", 0) / n if n else None, "avg_r_long": m.get("avg_r_long"),
                     "avg_r_short": m.get("avg_r_short"),
                     "winrate": m.get("winrate"), "avg_r": ev_r, "pf": m.get("pf"), "p_tp": m.get("p_tp"),
                     "sharpe_ann": m.get("sharpe_ann"), "max_dd_r": m.get("max_dd_r"),
                     "mfe_r_median": m.get("mfe_r_median"), "ok": bool(n >= min_trades)})
        log.info("вариант %d/%d: n=%s avg_r=%s", i + 1, len(combos), n, ev_r)
    df = pl.DataFrame(rows)
    if df.height:
        df = df.sort(["ok", "avg_r"], descending=[True, True], nulls_last=True)
    return df


def exit_study(trades: pl.DataFrame, *, tp_grid: list[float] = (0.5, 1.0, 1.5, 2.0, 3.0),
               sl_grid: list[float] = (0.5, 0.75, 1.0)) -> pl.DataFrame:
    """Оценка выходов по MFE/MAE без повторного прогона входов (раздел 7: выходы отдельно от входов).

    Грубая модель: TP в k·R достигнут, если MFE ≥ k и MAE < s (стоп в s·R не задет раньше);
    неоднозначные случаи — как стоп (консервативно). Итог — ожидание в R на сделку.
    """
    f = trades.filter(pl.col("status") == 0)
    mfe = f["mfe_r"].to_numpy()
    mae = f["mae_r"].to_numpy()
    out = []
    for s in sl_grid:
        for k in tp_grid:
            win = (mfe >= k) & (mae < s)
            loss = mae >= s
            flat = ~win & ~loss
            exp = (win * k - loss * s).sum() / max(1, len(f))
            out.append({"sl_r": s, "tp_r": k, "p_win": float(win.mean()) if len(f) else np.nan,
                        "p_loss": float(loss.mean()) if len(f) else np.nan, "p_flat": float(flat.mean()) if len(f) else np.nan,
                        "exp_r_per_risk": float(exp / s) if s else np.nan})
    return pl.DataFrame(out).sort("exp_r_per_risk", descending=True)
