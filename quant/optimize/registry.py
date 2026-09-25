"""Реестр испытаний и прогонов (раздел 7): каждая проверенная конфигурация — строка в БД."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import numpy as np
from sqlalchemy import func, select

from quant.db import session_scope
from quant.db.models import BacktestRun, Trial

ROOT = Path(__file__).resolve().parents[2]


def _jsonable(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {str(k): _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, (np.floating,)):
        v = float(obj)
        return None if v != v or v in (float("inf"), float("-inf")) else v
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, float):
        return None if obj != obj or obj in (float("inf"), float("-inf")) else obj
    if isinstance(obj, np.ndarray):
        return _jsonable(obj.tolist())
    return obj


def params_hash(params: dict) -> str:
    return hashlib.sha256(json.dumps(_jsonable(params), sort_keys=True).encode()).hexdigest()[:16]


@lru_cache(maxsize=1)
def code_version() -> str:
    """Хэш исходников quant/ (репозиторий может быть без коммитов)."""
    h = hashlib.sha256()
    for p in sorted((ROOT / "quant").rglob("*.py")):
        h.update(p.relative_to(ROOT).as_posix().encode())
        h.update(p.read_bytes())
    for p in sorted((ROOT / "config").glob("**/*.yaml")):
        h.update(p.read_bytes())
    return h.hexdigest()[:16]


def record_trial(*, study: str, level: str, strategy: str, params: dict, dataset_hash: str, metrics: dict,
                 group: str = "", window: str = "", block_perf: list | None = None) -> int:
    with session_scope() as s:
        t = Trial(study=study, level=level, strategy=strategy, group=group, params=_jsonable(params),
                  params_hash=params_hash(params), dataset_hash=dataset_hash, code_version=code_version(),
                  window=window, metrics=_jsonable(metrics), block_perf=_jsonable(block_perf or []))
        s.add(t)
        s.flush()
        return t.id


def count_trials(study: str) -> int:
    with session_scope() as s:
        return int(s.scalar(select(func.count(func.distinct(Trial.params_hash))).where(Trial.study == study)) or 0)


def record_run(*, kind: str, strategy: str, params: dict, dataset_hash: str, config: dict, metrics: dict,
               report_path: str | None = None, artifacts_dir: str | None = None) -> int:
    with session_scope() as s:
        r = BacktestRun(kind=kind, strategy=strategy, params=_jsonable(params), params_hash=params_hash(params),
                        dataset_hash=dataset_hash, code_version=code_version(), config=_jsonable(config),
                        metrics=_jsonable(metrics), report_path=report_path, artifacts_dir=artifacts_dir)
        s.add(r)
        s.flush()
        return r.id


def update_run(run_id: int, **fields: Any) -> None:
    with session_scope() as s:
        r = s.get(BacktestRun, run_id)
        for k, v in fields.items():
            setattr(r, k, _jsonable(v) if isinstance(v, (dict, list)) else v)
        if "params" in fields:
            r.params_hash = params_hash(fields["params"])


def holdout_used(strategy: str, params: dict) -> bool:
    with session_scope() as s:
        q = select(func.count()).select_from(BacktestRun).where(
            BacktestRun.kind == "holdout", BacktestRun.strategy == strategy,
            BacktestRun.params_hash == params_hash(params))
        return bool(s.scalar(q))
