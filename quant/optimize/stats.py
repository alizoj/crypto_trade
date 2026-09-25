"""Поправка на множественное тестирование (раздел 7): Deflated Sharpe Ratio и PBO (CSCV).

- DSR (Bailey & López de Prado, 2014): вероятность, что истинный Sharpe выбранной конфигурации > 0
  с учётом числа испытаний N и дисперсии Sharpe между испытаниями.
- PBO (Bailey, Borwein, López de Prado, Zhu): доля комбинаций разбиения, в которых лучший in-sample
  вариант оказывается в нижней половине out-of-sample.
"""

from __future__ import annotations

import math
from itertools import combinations

import numpy as np
from scipy import stats

EULER_GAMMA = 0.5772156649015329


def sharpe(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=float)
    if len(x) < 2:
        return 0.0
    sd = x.std(ddof=1)
    return float(x.mean() / sd) if sd > 0 else 0.0


def expected_max_sharpe(var_sr: float, n_trials: int) -> float:
    """Ожидаемый максимум Sharpe среди N испытаний при нулевом истинном Sharpe."""
    if n_trials <= 1 or var_sr <= 0:
        return 0.0
    z1 = stats.norm.ppf(1 - 1.0 / n_trials)
    z2 = stats.norm.ppf(1 - 1.0 / (n_trials * math.e))
    return math.sqrt(var_sr) * ((1 - EULER_GAMMA) * z1 + EULER_GAMMA * z2)


def deflated_sharpe(returns: np.ndarray, trial_sharpes: np.ndarray) -> dict:
    """DSR по ряду доходностей выбранной конфигурации (например, R по дням) и Sharpe всех испытаний
    (в тех же единицах: не аннуализированные)."""
    r = np.asarray(returns, dtype=float)
    T = len(r)
    sr = sharpe(r)
    ts = np.asarray(trial_sharpes, dtype=float)
    ts = ts[np.isfinite(ts)]
    n = max(1, len(ts))
    var_sr = float(ts.var(ddof=1)) if len(ts) > 1 else 0.0
    sr0 = expected_max_sharpe(var_sr, n)
    if T < 3:
        return {"dsr": float("nan"), "sr": sr, "sr0": sr0, "n_trials": n, "T": T}
    skew = float(stats.skew(r))
    kurt = float(stats.kurtosis(r, fisher=False))
    den = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    den = math.sqrt(den) if den > 0 else float("nan")
    z = (sr - sr0) * math.sqrt(T - 1) / den if den and den == den else float("nan")
    return {"dsr": float(stats.norm.cdf(z)) if z == z else float("nan"), "sr": sr, "sr0": sr0,
            "n_trials": n, "T": T, "skew": skew, "kurtosis": kurt}


def pbo_cscv(perf: np.ndarray, n_splits: int = 16, max_combos: int = 20_000, seed: int = 0) -> dict:
    """PBO по матрице производительности perf[T, N] (строки — время, столбцы — испытания).

    Время режется на n_splits блоков; для каждой комбинации половины блоков (IS) выбирается лучший
    по IS-среднему вариант, считается его относительный ранг на OOS и логит.
    """
    perf = np.asarray(perf, dtype=float)
    T, N = perf.shape
    if N < 2 or T < n_splits:
        return {"pbo": float("nan"), "n_combos": 0}
    blocks = np.array_split(np.arange(T), n_splits)
    block_mean = np.stack([np.nanmean(perf[b], axis=0) for b in blocks])      # (S, N)
    all_idx = list(range(n_splits))
    combos = list(combinations(all_idx, n_splits // 2))
    if len(combos) > max_combos:
        rng = np.random.default_rng(seed)
        combos = [combos[i] for i in rng.choice(len(combos), size=max_combos, replace=False)]
    logits = []
    for c in combos:
        is_mask = np.zeros(n_splits, dtype=bool)
        is_mask[list(c)] = True
        is_perf = block_mean[is_mask].mean(axis=0)
        oos_perf = block_mean[~is_mask].mean(axis=0)
        best = int(np.nanargmax(is_perf))
        rank = stats.rankdata(oos_perf)[best]          # 1..N, больше — лучше
        w = rank / (N + 1)
        logits.append(math.log(w / (1 - w)))
    lg = np.array(logits)
    return {"pbo": float((lg <= 0).mean()), "n_combos": len(combos), "logit_median": float(np.median(lg))}
