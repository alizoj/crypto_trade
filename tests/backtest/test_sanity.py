"""Приёмка фазы 4: санити-чек модели (раздел 9 ТЗ)."""

from __future__ import annotations

from quant.backtest.sanity import stage_a_fair, stage_b_fixed_risk, stage_b_governor


def test_fixed_risk_matches_gamblers_ruin():
    r = stage_b_fixed_risk(4_000, batch=500)
    assert r.ok, r.line()
    assert r.extra["unresolved"] <= 5


def test_default_governor_never_fails_in_90_days():
    r = stage_b_governor(400)
    assert r.ok, r.line()
    assert r.extra["p_unresolved"] > 0.2


def test_stage_a_unbiased_on_martingale():
    r = stage_a_fair(120_000)
    assert r.ok, r.line()
