"""Block bootstrap дневных последовательностей сделок (раздел 7) → доверительные интервалы для P(pass).

Из исторических сделок (ступень A) собирается синтетическая история той же длины: блоки по 3–5
последовательных проп-дней берутся из случайных мест истории и ставятся встык (время сделок, путей и
funding сдвигается). На каждой синтетической истории — rolling start и P(pass).
"""

from __future__ import annotations

from dataclasses import replace

import numpy as np

from quant.backtest.challenge import ChallengeResult, RiskParams, Stress, evaluate
from quant.backtest.sim import SimArrays
from quant.config import ChallengeConfig
from quant.timeutil import MIN_PER_DAY


def _ranges(starts: np.ndarray, lens: np.ndarray) -> np.ndarray:
    """Индексы всех элементов срезов [start, start+len)."""
    if len(starts) == 0:
        return np.zeros(0, np.int64)
    total = int(lens.sum())
    rep = np.repeat(starts - np.concatenate([[0], np.cumsum(lens)[:-1]]), lens)
    return rep + np.arange(total, dtype=np.int64)


def resample(arr: SimArrays, *, data_start_min: int, data_end_min: int, rng: np.random.Generator,
             block_min_days: int = 3, block_max_days: int = 5) -> SimArrays:
    """Одна синтетическая история той же длины из блоков исторических дней."""
    day0 = data_start_min // MIN_PER_DAY
    n_days = (data_end_min - data_start_min) // MIN_PER_DAY
    entry_day = arr.t_entry_min // MIN_PER_DAY - day0
    sel_idx: list[np.ndarray] = []
    shifts: list[np.ndarray] = []
    pos = 0
    while pos < n_days:
        L = int(rng.integers(block_min_days, block_max_days + 1))
        src = int(rng.integers(0, max(1, n_days - L)))
        idx = np.nonzero((entry_day >= src) & (entry_day < src + L))[0]
        sel_idx.append(idx)
        shifts.append(np.full(len(idx), (pos - src) * MIN_PER_DAY, dtype=np.int64))
        pos += L
    idx = np.concatenate(sel_idx) if sel_idx else np.zeros(0, np.int64)
    sh = np.concatenate(shifts) if shifts else np.zeros(0, np.int64)
    order = np.argsort(arr.t_entry_min[idx] + sh, kind="stable")
    idx, sh = idx[order], sh[order]
    # пути и funding: копии срезов со сдвигом времени
    p_idx = _ranges(arr.t_pstart[idx], arr.t_plen[idx])
    p_sh = np.repeat(sh, arr.t_plen[idx])
    f_idx = _ranges(arr.t_fstart[idx], arr.t_flen[idx])
    f_sh = np.repeat(sh, arr.t_flen[idx])
    new_pstart = np.concatenate([[0], np.cumsum(arr.t_plen[idx])[:-1]]).astype(np.int64) if len(idx) else np.zeros(0, np.int64)
    new_fstart = np.concatenate([[0], np.cumsum(arr.t_flen[idx])[:-1]]).astype(np.int64) if len(idx) else np.zeros(0, np.int64)

    def take(a: np.ndarray) -> np.ndarray:
        return np.ascontiguousarray(a[idx])

    x2 = arr.t_x2_min[idx]
    return replace(
        arr,
        t_entry_min=np.ascontiguousarray(arr.t_entry_min[idx] + sh), t_side=take(arr.t_side),
        t_entry_px=take(arr.t_entry_px), t_entry_slip=take(arr.t_entry_slip), t_entry_liq=take(arr.t_entry_liq),
        t_sl=take(arr.t_sl), t_risk=take(arr.t_risk), t_sym=take(arr.t_sym), t_group=take(arr.t_group),
        t_max_lot=take(arr.t_max_lot), t_max_total=take(arr.t_max_total), t_skip=take(arr.t_skip),
        t_x1_min=np.ascontiguousarray(arr.t_x1_min[idx] + sh), t_x1_px=take(arr.t_x1_px),
        t_x1_slip=take(arr.t_x1_slip), t_x1_frac=take(arr.t_x1_frac),
        t_x2_min=np.ascontiguousarray(np.where(x2 >= 0, x2 + sh, -1)), t_x2_px=take(arr.t_x2_px),
        t_x2_slip=take(arr.t_x2_slip), t_x2_frac=take(arr.t_x2_frac),
        t_pstart=new_pstart, t_plen=take(arr.t_plen),
        p_ts=np.ascontiguousarray(arr.p_ts[p_idx] + p_sh), p_wm_lo=np.ascontiguousarray(arr.p_wm_lo[p_idx]),
        p_wm_hi=np.ascontiguousarray(arr.p_wm_hi[p_idx]), p_mc=np.ascontiguousarray(arr.p_mc[p_idx]),
        p_wl_lo=np.ascontiguousarray(arr.p_wl_lo[p_idx]), p_wl_hi=np.ascontiguousarray(arr.p_wl_hi[p_idx]),
        p_lc=np.ascontiguousarray(arr.p_lc[p_idx]),
        t_fstart=new_fstart, t_flen=take(arr.t_flen),
        f_min=np.ascontiguousarray(arr.f_min[f_idx] + f_sh), f_amt=np.ascontiguousarray(arr.f_amt[f_idx]),
    )


def bootstrap_ci(arr: SimArrays, cfg: ChallengeConfig, *, data_start_min: int, data_end_min: int,
                 n_boot: int = 200, seed: int = 5, alpha: float = 0.05, starts_per_boot: int = 300,
                 metrics: tuple[str, ...] = ("p_pass_s1", "p_pass_both", "p_fail_daily"),
                 **eval_kwargs) -> dict:
    """Перцентильные ДИ метрик по block bootstrap."""
    rng = np.random.default_rng(seed)
    vals: dict[str, list[float]] = {k: [] for k in metrics}
    n_days = max(1, (data_end_min - data_start_min) // MIN_PER_DAY)
    eval_kwargs.setdefault("step_days", max(1, n_days // max(1, starts_per_boot)))
    for _ in range(n_boot):
        a = resample(arr, data_start_min=data_start_min, data_end_min=data_end_min, rng=rng)
        res: ChallengeResult = evaluate(a, cfg, data_start_min=data_start_min, data_end_min=data_end_min,
                                        include_trader=False, **eval_kwargs)
        for k in metrics:
            if k in res.metrics:
                vals[k].append(res.metrics[k])
    out = {}
    for k, v in vals.items():
        if v:
            a = np.array(v)
            out[k] = {"mean": float(a.mean()), "lo": float(np.quantile(a, alpha / 2)),
                      "hi": float(np.quantile(a, 1 - alpha / 2)), "n_boot": len(a)}
    return out


def stress_table(arr: SimArrays, cfg: ChallengeConfig, presets: dict, **eval_kwargs) -> dict[str, dict]:
    """Метрики челленджа по стресс-сценариям (комиссии ×1.5, slippage ×2, пропуски, equity по last...)."""
    return {name: evaluate(arr, cfg, stress=st, include_trader=False, **eval_kwargs).metrics
            for name, st in presets.items()}


__all__ = ["bootstrap_ci", "resample", "stress_table", "RiskParams", "Stress"]
