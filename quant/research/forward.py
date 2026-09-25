"""Проверка зарегистрированных кандидатов на новых данных (config/forward/candidates.yaml).

Кандидат: стратегия + переопределения сетапа + переопределения контроля (контроль = сетап + поля контроля, т.е.
те же правила, выходы и фильтр — меняется только способ входа). Считаются сделки с даты start реестра до конца
данных; для случайных входов — по каждому seed. Вердикт по критерию выносится, только когда у сетапа >= min_trades.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from quant.config import CONFIG_DIR, load_challenge_config
from quant.instruments import instruments
from quant.timeutil import parse_dt

log = logging.getLogger(__name__)
DEFAULT_PATH = CONFIG_DIR / "forward" / "candidates.yaml"
CRITERIA = ("setup_gt_control_and_positive",)


@dataclass
class Candidate:
    name: str
    strategy: str
    groups: list[str]
    setup: dict[str, Any]
    control: dict[str, Any]
    criterion: str
    note: str = ""

    @property
    def symbols(self) -> list[str]:
        return [i.hh_symbol for i in instruments() if i.group in self.groups]

    def control_overrides(self) -> dict[str, Any]:
        return {**self.setup, **self.control}


@dataclass
class Registry:
    registered: str
    start: datetime
    min_trades: int
    seeds: list[int]
    candidates: list[Candidate] = field(default_factory=list)


def load_registry(path: Path | None = None) -> Registry:
    raw = yaml.safe_load(Path(path or DEFAULT_PATH).read_text(encoding="utf-8"))
    cands = [Candidate(name=c["name"], strategy=c["strategy"], groups=list(c["groups"]), setup=dict(c["setup"]),
                       control=dict(c["control"]), criterion=c["criterion"], note=c.get("note", ""))
             for c in raw["candidates"]]
    for c in cands:
        if c.criterion not in CRITERIA:
            raise ValueError(f"{c.name}: неизвестный критерий {c.criterion!r}, есть {CRITERIA}")
    return Registry(registered=str(raw["registered"]), start=parse_dt(str(raw["start"])),
                    min_trades=int(raw.get("min_trades", 100)), seeds=[int(s) for s in raw.get("seeds", [42, 1, 2])],
                    candidates=cands)


def _variants(overrides: dict, seeds: list[int]) -> list[dict]:
    """Случайный вход — по каждому seed; детерминированный — один прогон."""
    if overrides.get("entry_mode") == "random":
        return [{**overrides, "seed": s} for s in seeds]
    return [overrides]


def _stats(r: np.ndarray) -> dict:
    n = int(len(r))
    return {"n": n, "r": float(r.mean()) if n else None,
            "se": float(r.std(ddof=1) / np.sqrt(n)) if n > 1 else None, "sum_r": float(r.sum()) if n else 0.0}


def evaluate_candidate(c: Candidate, reg: Registry, end: datetime) -> dict:
    from quant.backtest.engine import BoundedBarsCache, BuildSpec, build_tradeset

    cfg = load_challenge_config()

    def run(ov: dict) -> np.ndarray:
        try:
            ts = build_tradeset(BuildSpec(strategy=c.strategy, symbols=c.symbols, start=reg.start, end=end,
                                          overrides=ov, record_path=False, use_book_depth=False),
                                cfg=cfg, bars_cache=BoundedBarsCache(4e9))
        except ValueError:                     # нет ни одного сигнала за период
            return np.zeros(0)
        return ts.filled["r_net"].drop_nulls().to_numpy()

    setup_runs = [run(v) for v in _variants(c.setup, reg.seeds)]
    ctrl_runs = [run(v) for v in _variants(c.control_overrides(), reg.seeds)]
    s = {**_stats(np.concatenate(setup_runs)), "runs": len(setup_runs)}      # случайный сетап — сделки всех seed
    ctrl = [_stats(x) for x in ctrl_runs]
    ctrl_mean = float(np.mean([x["r"] for x in ctrl if x["r"] is not None])) if any(x["r"] is not None for x in ctrl) else None
    if s["n"] / s["runs"] < reg.min_trades or s["r"] is None or ctrl_mean is None:
        verdict = "накапливается"
    else:
        verdict = "выполнен" if (s["r"] > 0 and s["r"] > ctrl_mean) else "НЕ выполнен"
    return {"name": c.name, "setup": s, "control": ctrl, "control_mean_r": ctrl_mean, "verdict": verdict}


def evaluate_all(reg: Registry, end: datetime, names: list[str] | None = None) -> list[dict]:
    out = []
    for c in reg.candidates:
        if names and c.name not in names:
            continue
        log.info("forward: %s", c.name)
        out.append(evaluate_candidate(c, reg, end))
    return out
