"""Walk-forward оптимизация (раздел 7 ТЗ).

- окна train/test (по умолчанию 6 мес / 2 мес, шаг 2 мес); последние 6 мес — hold-out, не трогать;
- purge: в train только сделки, закрытые до конца train; embargo: test начинается через ≥ max_hold после train;
- цель: P(pass both) (приближение Π P(stage) для коротких окон) при ограничении P(fail by daily) ≤ порога;
- выбор по плато (среднее по соседям в сетке) со штрафом за смену параметров между окнами;
- OOS: сделки test-окон с выбранными параметрами сшиваются и оцениваются как челлендж (rolling start);
- каждый вариант пишется в реестр испытаний; DSR и PBO — по всем вариантам.
"""

from __future__ import annotations

import copy
import itertools
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import numpy as np
import polars as pl

from quant.backtest.challenge import RiskParams, evaluate, evaluate_independent
from quant.backtest.metrics import classic_metrics, daily_r
from quant.backtest.tradeset import TradeSet, concat, select, to_sim_arrays
from quant.config import ChallengeConfig
from quant.optimize import registry
from quant.optimize.stats import deflated_sharpe, pbo_cscv, sharpe
from quant.timeutil import MIN_PER_DAY

log = logging.getLogger(__name__)
MONTH_MIN = int(30.44 * MIN_PER_DAY)


def set_path(params: dict, path: str, value: Any) -> dict:
    out = copy.deepcopy(params)
    cur = out
    keys = path.split(".")
    for k in keys[:-1]:
        cur = cur.setdefault(k, {})
    cur[keys[-1]] = value
    return out


def grid(base: dict, search_space: dict[str, list], max_combos: int | None = None, seed: int = 0) -> list[dict]:
    keys = list(search_space)
    combos = []
    for values in itertools.product(*[search_space[k] for k in keys]):
        p = base
        for k, v in zip(keys, values):
            p = set_path(p, k, v)
        combos.append({"params": p, "point": dict(zip(keys, values))})
    if max_combos and len(combos) > max_combos:
        rng = np.random.default_rng(seed)
        idx = sorted(rng.choice(len(combos), size=max_combos, replace=False))
        combos = [combos[i] for i in idx]
    return combos


@dataclass
class Window:
    idx: int
    train_start: int
    train_end: int
    test_start: int
    test_end: int

    @property
    def label(self) -> str:
        from quant.timeutil import from_min

        f = lambda m: from_min(m).strftime("%Y-%m-%d")  # noqa: E731
        return f"{f(self.train_start)}..{f(self.train_end)} → {f(self.test_start)}..{f(self.test_end)}"


def make_windows(data_start: int, holdout_start: int, *, train_months: float = 6, test_months: float = 2,
                 step_months: float = 2, embargo_min: int = 1440) -> list[Window]:
    out = []
    t = data_start
    k = 0
    while True:
        tr_end = t + int(train_months * MONTH_MIN)
        te_start = tr_end + embargo_min
        te_end = te_start + int(test_months * MONTH_MIN)
        if te_end > holdout_start:
            break
        out.append(Window(k, t, tr_end, te_start, te_end))
        k += 1
        t += int(step_months * MONTH_MIN)
    return out


@dataclass
class WFConfig:
    challenge_type: str = "two_stage"
    size: float = 10_000
    horizon_days: float = 90
    fail_daily_max: float = 0.35
    objective: str = "p_pass_both_approx"
    stability_penalty: float = 0.02
    holdout_months: float = 6
    train_months: float = 6
    test_months: float = 2
    step_months: float = 2
    max_combos: int | None = None
    step_days: int = 1


@dataclass
class WFResult:
    study: str
    windows: list[Window]
    combos: list[dict]
    train_table: pl.DataFrame
    chosen: list[int]
    oos_trades: TradeSet | None
    oos_metrics: dict
    oos_classic: dict
    final_combo: int
    dsr: dict
    pbo: dict
    holdout_start: int
    extra: dict = field(default_factory=dict)


def _neighbors(combos: list[dict], space: dict[str, list]) -> list[list[int]]:
    keys = list(space)
    pos = [{k: space[k].index(c["point"][k]) for k in keys} for c in combos]
    index = {tuple(p[k] for k in keys): i for i, p in enumerate(pos)}
    out = []
    for p in pos:
        nb = []
        for k_i, k in enumerate(keys):
            for d in (-1, 1):
                q = [p[kk] for kk in keys]
                q[k_i] += d
                j = index.get(tuple(q))
                if j is not None:
                    nb.append(j)
        out.append(nb)
    return out


def _objective(m: dict, cfg: WFConfig) -> float:
    if m.get("n_starts", 0) == 0:
        return float("-inf")
    if m.get("p_fail_daily_max", m.get("p_fail_daily", 0.0)) > cfg.fail_daily_max:
        return float("-inf")
    return float(m.get(cfg.objective, float("nan")))


def run_walkforward(*, study: str, strategy: str, make_tradeset: Callable[[dict], TradeSet], base_params: dict,
                    search_space: dict[str, list], chall_cfg: ChallengeConfig, risk: RiskParams, wf: WFConfig,
                    data_start: int, data_end: int, dataset_hash: str, embargo_min: int, group: str = "",
                    ) -> WFResult:
    holdout_start = data_end - int(wf.holdout_months * MONTH_MIN)
    windows = make_windows(data_start, holdout_start, train_months=wf.train_months, test_months=wf.test_months,
                           step_months=wf.step_months, embargo_min=embargo_min)
    if not windows:
        raise ValueError("недостаточно истории для walk-forward: нужно train + test + hold-out")
    combos = grid(base_params, search_space, wf.max_combos)
    log.info("walk-forward %s: %d окон × %d вариантов", study, len(windows), len(combos))
    rows = []
    daily: dict[int, pl.DataFrame] = {}
    full_obj: list[float] = []
    for ci, c in enumerate(combos):
        # полный набор отпускаем сразу: на всём пуле один вариант — гигабайты путей
        ts_nh = select(make_tradeset(c["params"]),
                       (pl.col("entry_min") < holdout_start) & (pl.col("exit_min") < holdout_start))
        daily[ci] = daily_r(ts_nh.trades)
        cls_full = classic_metrics(ts_nh.trades)
        for w in windows:
            tr = select(ts_nh, (pl.col("entry_min") >= w.train_start) & (pl.col("exit_min") < w.train_end))
            m = evaluate_independent(to_sim_arrays(tr), chall_cfg, challenge_type=wf.challenge_type, size=wf.size,
                                     risk=risk, horizon_days=wf.horizon_days, data_start_min=w.train_start,
                                     data_end_min=w.train_end, step_days=wf.step_days)
            cls = classic_metrics(tr.trades)
            obj = _objective(m, wf)
            rows.append({"combo": ci, "window": w.idx, "objective": obj, **{k: v for k, v in m.items()},
                         "n_trades": cls.get("n_trades", 0), "avg_r": cls.get("avg_r")})
            registry.record_trial(study=study, level="event", strategy=strategy, params=c["params"],
                                  dataset_hash=dataset_hash, group=group, window=f"train:{w.label}",
                                  metrics={**m, "objective": obj, **{f"classic_{k}": v for k, v in cls.items()}})
        # полный период без hold-out — для финального выбора
        mf = evaluate_independent(to_sim_arrays(ts_nh), chall_cfg, challenge_type=wf.challenge_type, size=wf.size,
                                  risk=risk, horizon_days=wf.horizon_days, data_start_min=data_start,
                                  data_end_min=holdout_start, step_days=wf.step_days)
        full_obj.append(_objective(mf, wf))
        monthly = _monthly_perf(daily[ci], data_start, holdout_start)
        registry.record_trial(study=study, level="event", strategy=strategy, params=c["params"],
                              dataset_hash=dataset_hash, group=group, window="full",
                              metrics={**mf, "objective": full_obj[-1], **{f"classic_{k}": v for k, v in cls_full.items()}},
                              block_perf=monthly.tolist())
        log.info("вариант %d/%d: objective(full)=%.3f", ci + 1, len(combos), full_obj[-1])
        del ts_nh, tr
    table = pl.DataFrame(rows)
    nbrs = _neighbors(combos, search_space) if wf.max_combos is None else [[] for _ in combos]

    # выбор по окнам: плато + штраф за смену параметров
    chosen: list[int] = []
    prev: int | None = None
    for w in windows:
        obj = np.full(len(combos), -np.inf)
        sub = table.filter(pl.col("window") == w.idx)
        for r in sub.iter_rows(named=True):
            obj[r["combo"]] = r["objective"]
        chosen.append(_choose(obj, nbrs, combos, prev, wf.stability_penalty))
        prev = chosen[-1]
    final = _choose(np.array(full_obj), nbrs, combos, None, 0.0)

    # OOS: сшивка test-окон. Каждый выбранный вариант строится один раз и сразу отпускается —
    # держать наборы всех выбранных вариантов одновременно на большом пуле не хватит памяти
    by_window: dict[int, TradeSet] = {}
    for ci in sorted({ci for ci in chosen if ci >= 0}):
        ts = make_tradeset(combos[ci]["params"])
        for w, cw in zip(windows, chosen):
            if cw != ci:
                continue
            part = select(ts, (pl.col("entry_min") >= w.test_start) & (pl.col("entry_min") < w.test_end)
                          & (pl.col("exit_min") < holdout_start))
            if part.trades.height:
                by_window[w.idx] = part
        del ts
    parts = [by_window[k] for k in sorted(by_window)]
    oos = concat(parts, meta={"data_start_min": windows[0].test_start, "data_end_min": windows[-1].test_end}) if parts else None
    oos_metrics: dict = {}
    oos_classic: dict = {}
    if oos is not None and oos.trades.height:
        oos_classic = classic_metrics(oos.trades)
        res = evaluate(to_sim_arrays(oos), chall_cfg, challenge_type=wf.challenge_type, size=wf.size, risk=risk,
                       horizon_days=wf.horizon_days, data_start_min=windows[0].test_start,
                       data_end_min=windows[-1].test_end, include_trader=False, step_days=wf.step_days)
        oos_metrics = res.metrics
        if oos_metrics.get("n_starts", 0) == 0:
            oos_metrics.update({f"indep_{k}": v for k, v in evaluate_independent(
                to_sim_arrays(oos), chall_cfg, challenge_type=wf.challenge_type, size=wf.size, risk=risk,
                horizon_days=min(wf.horizon_days, (windows[-1].test_end - windows[0].test_start) / MIN_PER_DAY / 2),
                data_start_min=windows[0].test_start, data_end_min=windows[-1].test_end).items()})

    # DSR и PBO по всем вариантам
    trial_sr = np.array([sharpe(_aligned(daily[ci], data_start, holdout_start)) for ci in range(len(combos))])
    dsr = deflated_sharpe(_aligned(daily[final], data_start, holdout_start), trial_sr) if final >= 0 else {}
    perf = np.column_stack([_aligned(daily[ci], data_start, holdout_start) for ci in range(len(combos))])
    pbo = pbo_cscv(perf) if len(combos) > 1 else {"pbo": float("nan")}
    return WFResult(study=study, windows=windows, combos=combos, train_table=table, chosen=chosen, oos_trades=oos,
                    oos_metrics=oos_metrics, oos_classic=oos_classic, final_combo=final, dsr=dsr, pbo=pbo,
                    holdout_start=holdout_start,
                    extra={"full_objective": full_obj, "sensitivity": _sensitivity(np.array(full_obj), nbrs, final)})


def _choose(obj: np.ndarray, nbrs: list[list[int]], combos: list[dict], prev: int | None, penalty: float) -> int:
    if not np.isfinite(obj).any():
        return -1
    smooth = np.full(len(obj), -np.inf)
    for i in range(len(obj)):
        if not np.isfinite(obj[i]):
            continue
        vals = [obj[i]] + [obj[j] for j in nbrs[i] if np.isfinite(obj[j])]
        smooth[i] = float(np.mean(vals))
        if prev is not None and prev >= 0 and penalty:
            a, b = combos[i]["point"], combos[prev]["point"]
            changed = sum(1 for k in a if a[k] != b[k]) / max(1, len(a))
            smooth[i] -= penalty * changed
    return int(np.argmax(smooth))


def _sensitivity(obj: np.ndarray, nbrs: list[list[int]], i: int) -> dict:
    if i < 0:
        return {}
    vals = [obj[j] for j in nbrs[i] if np.isfinite(obj[j])]
    return {"objective": float(obj[i]), "neighbors_min": float(min(vals)) if vals else None,
            "neighbors_max": float(max(vals)) if vals else None, "n_neighbors": len(vals)}


def _aligned(d: pl.DataFrame, start: int, end: int, boundary_min: int = 1200) -> np.ndarray:
    off = 1440 - boundary_min
    d0, d1 = (start + off) // 1440, (end + off) // 1440
    out = np.zeros(max(1, d1 - d0))
    if d.height:
        days = d["day"].to_numpy() - d0
        ok = (days >= 0) & (days < len(out))
        out[days[ok]] = d["r"].to_numpy()[ok]
    return out


def _monthly_perf(d: pl.DataFrame, start: int, end: int) -> np.ndarray:
    a = _aligned(d, start, end)
    n = max(1, len(a) // 30)
    return np.array([x.mean() for x in np.array_split(a, n)])
