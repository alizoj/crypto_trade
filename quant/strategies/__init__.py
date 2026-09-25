"""Реестр стратегий и загрузка параметров из config/strategies/<name>.yaml.

load_params(name, group) = params + groups.<group> (рекурсивное слияние) + filters
(верхнеуровневая секция filters, поверх неё — groups.<group>.filters) + tf/htf из yaml.
Секции risk / execution читает движок бэктеста напрямую из yaml (load_strategy_config).
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

from quant.config import load_strategy_config
from quant.strategies.base import Context, Factor, Signal, Strategy
from quant.strategies.baseline_random import BaselineRandom
from quant.strategies.common import deep_merge
from quant.strategies.confluence_v1 import ConfluenceV1
from quant.strategies.rsi_pin_ew import RsiPinEw

STRATEGIES: dict[str, type[Strategy]] = {
    ConfluenceV1.name: ConfluenceV1,
    BaselineRandom.name: BaselineRandom,
    RsiPinEw.name: RsiPinEw,
}


def load_params(name: str, group: str | None = None, directory: Path | None = None) -> dict[str, Any]:
    cfg = load_strategy_config(name, directory)
    params = deepcopy(cfg.get("params") or {})
    over = deepcopy(((cfg.get("groups") or {}).get(group) or {}) if group else {})
    group_filters = over.pop("filters", None) or {}
    params = deep_merge(params, over)
    filters = deep_merge(cfg.get("filters") or {}, params.pop("filters", None) or {})
    params["filters"] = deep_merge(filters, group_filters)
    for key in ("tf", "htf"):
        if cfg.get(key) is not None:
            params.setdefault(key, cfg[key])
    return params


def set_dotted(params: dict[str, Any], path: str, value: Any) -> dict[str, Any]:
    """Копия params с изменённым значением по пути 'long.threshold' (для search_space)."""
    out = deepcopy(params)
    node = out
    keys = path.split(".")
    for k in keys[:-1]:
        node = node.setdefault(k, {})
    node[keys[-1]] = value
    return out


def with_overrides(params: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    out = params
    for path, value in overrides.items():
        out = set_dotted(out, path, value)
    return out


def get_strategy(name: str, params: dict[str, Any] | None = None, group: str | None = None) -> Strategy:
    """Экземпляр стратегии. params=None — из yaml (с переопределениями группы)."""
    try:
        cls = STRATEGIES[name]
    except KeyError:
        raise KeyError(f"неизвестная стратегия {name!r}; есть: {sorted(STRATEGIES)}") from None
    if params is None:
        params = load_params(name, group)
    return cls(params)


__all__ = [
    "STRATEGIES", "BaselineRandom", "ConfluenceV1", "Context", "Factor", "Signal", "Strategy",
    "get_strategy", "load_params", "set_dotted", "with_overrides",
]
