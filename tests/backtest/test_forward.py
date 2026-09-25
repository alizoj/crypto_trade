"""Реестр кандидатов для проверки на новых данных: загрузка, валидность правил, контроль = сетап + способ входа."""

from __future__ import annotations

import pytest

from quant.research.forward import Candidate, _variants, load_registry
from quant.strategies import get_strategy, load_params, with_overrides


def test_registry_loads_and_rules_are_valid():
    reg = load_registry()
    assert reg.start.isoformat().startswith("2026-09-25") and reg.min_trades >= 50
    assert {c.name for c in reg.candidates} >= {"H-B trend majors", "Squeeze longs", "Cascade shorts"}
    for c in reg.candidates:
        assert c.symbols, c.name
        for ov in (c.setup, c.control_overrides()):
            get_strategy(c.strategy, with_overrides(load_params(c.strategy, "crypto_alt"), ov))   # правила валидны


def test_control_inherits_setup():
    c = Candidate(name="x", strategy="feature_rules", groups=["crypto_major"],
                  setup={"rules_long": [["rsi14", "<=", 30]], "breadth_min_frac": 0.0, "tp_r": 3.0},
                  control={"entry_mode": "random", "random_signals_per_day": 0.1}, criterion="setup_gt_control_and_positive")
    ov = c.control_overrides()
    assert ov["rules_long"] == [["rsi14", "<=", 30]] and ov["tp_r"] == 3.0 and ov["breadth_min_frac"] == 0.0
    assert ov["entry_mode"] == "random"


def test_variants_by_seed():
    assert _variants({"entry_mode": "random"}, [1, 2, 3]) == [{"entry_mode": "random", "seed": s} for s in (1, 2, 3)]
    assert _variants({"rules_long": []}, [1, 2, 3]) == [{"rules_long": []}]


def test_unknown_criterion_rejected(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("registered: '2026-09-26'\nstart: '2026-09-25'\ncandidates:\n"
                 "  - {name: a, strategy: feature_rules, groups: [crypto_major], setup: {}, control: {}, criterion: nope}\n",
                 encoding="utf-8")
    with pytest.raises(ValueError):
        load_registry(p)
