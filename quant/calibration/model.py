"""Калибровка score → P(TP раньше SL) (раздел 8.1 ТЗ).

- только на out-of-sample сделках walk-forward;
- отдельно по (группа, сторона, ТФ): crypto_major / crypto_alt / tradfi × long/short;
- изотоническая регрессия; корзина = ступенька изотонической функции (для n и Wilson-ДИ);
- успех = TP1 достигнут раньше SL в пределах max_hold; таймаут — не успех (консервативно);
- при n < min_n — «мало данных»; без модели — «не откалибровано».
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import polars as pl
from sklearn.isotonic import IsotonicRegression
from sqlalchemy import select, update

from quant.db import session_scope
from quant.db.models import CalibrationModel

MIN_N_DEFAULT = 100


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n <= 0:
        return (0.0, 1.0)
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return (max(0.0, c - h), min(1.0, c + h))


@dataclass
class Calibrator:
    x: list[float]
    y: list[float]
    blocks: list[dict]                   # {lo, hi, p, n, k}
    n_total: int
    min_n: int = MIN_N_DEFAULT
    metrics: dict = field(default_factory=dict)

    def predict_p(self, score: float) -> float:
        return float(np.interp(score, self.x, self.y))

    def block_for(self, score: float) -> dict:
        best, dist = self.blocks[0], float("inf")
        for b in self.blocks:
            if b["lo"] <= score <= b["hi"]:
                return b
            d = min(abs(score - b["lo"]), abs(score - b["hi"]))
            if d < dist:
                best, dist = b, d
        return best

    def prob_info(self, score: float) -> dict:
        p = self.predict_p(score)
        b = self.block_for(score)
        lo, hi = wilson(b["k"], b["n"])
        return {"p": p, "ci_low": lo, "ci_high": hi, "n": int(b["n"]), "calibrated": True,
                "low_data": b["n"] < self.min_n}

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "blocks": self.blocks, "n_total": self.n_total, "min_n": self.min_n}

    @classmethod
    def from_dict(cls, d: dict, metrics: dict | None = None) -> "Calibrator":
        return cls(x=d["x"], y=d["y"], blocks=d["blocks"], n_total=d["n_total"], min_n=d.get("min_n", MIN_N_DEFAULT),
                   metrics=metrics or {})


def fit(scores: np.ndarray, success: np.ndarray, *, min_n: int = MIN_N_DEFAULT) -> Calibrator:
    s = np.asarray(scores, dtype=float)
    y = np.asarray(success, dtype=float)
    ok = np.isfinite(s)
    s, y = s[ok], y[ok]
    iso = IsotonicRegression(increasing=True, out_of_bounds="clip", y_min=0.0, y_max=1.0)
    iso.fit(s, y)
    pred = iso.predict(s)
    blocks = []
    for pv in np.unique(pred):
        m = pred == pv
        blocks.append({"lo": float(s[m].min()), "hi": float(s[m].max()), "p": float(pv),
                       "n": int(m.sum()), "k": int(y[m].sum())})
    blocks.sort(key=lambda b: b["lo"])
    cal = Calibrator(x=[float(v) for v in iso.X_thresholds_], y=[float(v) for v in iso.y_thresholds_],
                     blocks=blocks, n_total=int(len(s)), min_n=min_n)
    cal.metrics = {"brier": brier(pred, y), "brier_base": brier(np.full(len(y), y.mean()), y),
                   "base_rate": float(y.mean()), "n": int(len(y))}
    return cal


def brier(pred: np.ndarray, obs: np.ndarray) -> float:
    pred = np.asarray(pred, dtype=float)
    obs = np.asarray(obs, dtype=float)
    return float(np.mean((pred - obs) ** 2)) if len(obs) else float("nan")


def reliability(pred: np.ndarray, obs: np.ndarray, n_bins: int = 10) -> list[dict]:
    """Корзины по предсказанной вероятности: средняя предсказанная vs фактическая частота."""
    pred = np.asarray(pred, dtype=float)
    obs = np.asarray(obs, dtype=float)
    edges = np.linspace(0, 1, n_bins + 1)
    out = []
    for i in range(n_bins):
        m = (pred >= edges[i]) & ((pred < edges[i + 1]) if i < n_bins - 1 else (pred <= edges[i + 1]))
        n = int(m.sum())
        if n == 0:
            continue
        k = int(obs[m].sum())
        lo, hi = wilson(k, n)
        out.append({"p_pred": float(pred[m].mean()), "p_obs": k / n, "n": n, "ci_low": lo, "ci_high": hi})
    return out


def reliability_ok(buckets: list[dict], *, tol: float = 0.05, min_n: int = 100) -> bool:
    """Приёмка фазы 6: |pred − obs| ≤ 5 п.п. в корзинах с n ≥ 100."""
    big = [b for b in buckets if b["n"] >= min_n]
    return bool(big) and all(abs(b["p_pred"] - b["p_obs"]) <= tol for b in big)


def ev_r(p: float, rr: float, fees_r: float) -> float:
    """EV в R = P × RR − (1 − P) − fees_R (раздел 8.1)."""
    return p * rr - (1 - p) - fees_r


Key = tuple[str, str, str]   # (group, side, tf)


def fit_from_trades(trades: pl.DataFrame, *, min_n: int = MIN_N_DEFAULT, min_fit: int = 30) -> dict[Key, Calibrator]:
    """trades: OOS-сделки (колонки score, label, side, group, tf, status)."""
    f = trades.filter((pl.col("status") == 0) & (pl.col("label") >= 0))
    out: dict[Key, Calibrator] = {}
    for (group, side, tf), sub in f.group_by(["group", "side", "tf"]):
        if sub.height < min_fit:
            continue
        side_s = "long" if side == 1 else "short"
        cal = fit(sub["score"].to_numpy(), (sub["label"] == 1).to_numpy().astype(float), min_n=min_n)
        out[(str(group), side_s, str(tf))] = cal
    return out


def save_models(models: dict[Key, Calibrator], *, strategy: str, source_run_id: int | None = None) -> list[int]:
    ids = []
    with session_scope() as s:
        for (group, side, tf), cal in models.items():
            prev = s.scalars(select(CalibrationModel).where(
                CalibrationModel.strategy == strategy, CalibrationModel.group == group,
                CalibrationModel.side == side, CalibrationModel.tf == tf)).all()
            version = max((p.version for p in prev), default=0) + 1
            s.execute(update(CalibrationModel).where(
                CalibrationModel.strategy == strategy, CalibrationModel.group == group,
                CalibrationModel.side == side, CalibrationModel.tf == tf).values(active=False))
            row = CalibrationModel(strategy=strategy, group=group, side=side, tf=tf, version=version, active=True,
                                   method="isotonic", model=cal.to_dict(), metrics=cal.metrics,
                                   source_run_id=source_run_id)
            s.add(row)
            s.flush()
            ids.append(row.id)
    return ids


def load_active(strategy: str) -> dict[Key, Calibrator]:
    with session_scope() as s:
        rows = s.scalars(select(CalibrationModel).where(CalibrationModel.strategy == strategy,
                                                        CalibrationModel.active.is_(True))).all()
        return {(r.group, r.side, r.tf): Calibrator.from_dict(r.model, r.metrics) for r in rows}


def prob_for(models: dict[Key, Calibrator], group: str, side: str, tf: str, score: float | None) -> dict | None:
    if score is None or not np.isfinite(score):
        return None
    cal = models.get((group, side, tf))
    if cal is None:
        return None
    return cal.prob_info(float(score))


def live_check(pred: np.ndarray, obs: np.ndarray, *, tol: float = 0.10, min_n: int = 30) -> dict[str, Any]:
    """Контроль калибровки на live-сигналах: при расхождении > tol в корзине с n ≥ min_n — stale."""
    buckets = reliability(pred, obs, n_bins=5)
    stale = any(abs(b["p_pred"] - b["p_obs"]) > tol for b in buckets if b["n"] >= min_n)
    return {"buckets": buckets, "brier": brier(pred, obs) if len(obs) else None, "n_total": int(len(obs)),
            "stale": bool(stale)}
