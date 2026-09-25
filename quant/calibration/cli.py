"""CLI: quant calibrate fit | check (фаза 6, раздел 8.1)."""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import polars as pl


def _load_trades(run_id: int, sub: str) -> tuple[pl.DataFrame, str]:
    from quant.db import session_scope
    from quant.db.models import BacktestRun

    with session_scope() as s:
        run = s.get(BacktestRun, run_id)
        if run is None or not run.artifacts_dir:
            raise SystemExit(f"нет прогона #{run_id} с артефактами")
        strategy = run.strategy
        path = Path(run.artifacts_dir) / sub / "trades.parquet"
    if not path.exists():
        raise SystemExit(f"нет файла {path}")
    return pl.read_parquet(path), strategy


def _fit(args: argparse.Namespace) -> int:
    from quant.calibration.model import fit_from_trades, save_models

    trades, strategy = _load_trades(args.wf_run, "oos")
    models = fit_from_trades(trades, min_n=args.min_n)
    if not models:
        raise SystemExit("недостаточно OOS-сделок для калибровки")
    ids = save_models(models, strategy=strategy, source_run_id=args.wf_run)
    for (g, s, tf), cal in models.items():
        print(f"{g}/{s}/{tf}: n={cal.n_total} brier={cal.metrics['brier']:.4f} (база {cal.metrics['brier_base']:.4f}) "
              f"корзин {len(cal.blocks)}")
    print(f"сохранено моделей: {len(ids)} (активны)")
    return 0


def _check(args: argparse.Namespace) -> int:
    from quant.calibration.model import load_active, reliability, reliability_ok

    trades, strategy = _load_trades(args.run, "trades")
    models = load_active(strategy)
    f = trades.filter((pl.col("status") == 0) & (pl.col("label") >= 0))
    pred, obs = [], []
    for r in f.iter_rows(named=True):
        cal = models.get((r["group"], "long" if r["side"] == 1 else "short", r["tf"]))
        if cal is None:
            continue
        pred.append(cal.predict_p(r["score"]))
        obs.append(1.0 if r["label"] == 1 else 0.0)
    buckets = reliability(np.array(pred), np.array(obs))
    for b in buckets:
        print(f"pred {b['p_pred']:.3f}  obs {b['p_obs']:.3f}  n={b['n']}  ДИ {b['ci_low']:.3f}–{b['ci_high']:.3f}")
    ok = reliability_ok(buckets)
    print("приёмка фазы 6 (±5 п.п. в корзинах с n≥100):", "OK" if ok else "НЕ выполнена / мало данных")
    return 0 if ok else 1


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("calibrate", help="калибровка score → P на OOS-сделках walk-forward")
    s = p.add_subparsers(dest="cmd", required=True)
    f = s.add_parser("fit")
    f.add_argument("--wf-run", type=int, required=True, help="id walk-forward прогона (OOS-сделки)")
    f.add_argument("--min-n", type=int, default=100)
    f.set_defaults(func=_fit)
    c = s.add_parser("check", help="reliability активных моделей на сделках прогона (hold-out)")
    c.add_argument("--run", type=int, required=True)
    c.set_defaults(func=_check)
