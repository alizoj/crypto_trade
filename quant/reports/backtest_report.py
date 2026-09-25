"""Отчёт по прогону бэктеста (раздел 7): метрики челленджа, baseline, стресс, кривые, классика."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from quant.backtest import sim as S
from quant.backtest.challenge import ChallengeResult, RiskParams
from quant.backtest.metrics import by_group
from quant.backtest.tradeset import TradeSet, to_sim_arrays
from quant.config import ChallengeConfig
from quant.data import store
from quant.optimize.registry import code_version
from quant.reports.html import histogram, render, svg_bars, svg_lines, table, tile
from quant.timeutil import MIN_PER_DAY, from_min

STATUS_COLOR = {S.ST_PASS: "var(--good)", S.ST_FAIL: "var(--critical)", S.ST_OPEN: "var(--neutral)"}
STATUS_NAME = {S.ST_PASS: "прошёл", S.ST_FAIL: "провал", S.ST_OPEN: "не завершён"}
REASON_NAME = {S.R_DAILY: "дневной лимит", S.R_TOTAL: "общий лимит", S.R_LIQ: "ликвидация"}

KEY_ROWS = [
    ("P(pass stage1)", "p_pass_s1", "pct"), ("P(pass stage2)", "p_pass_s2", "pct"),
    ("P(pass both)", "p_pass_both", "pct"), ("P(fail daily)", "p_fail_daily", "pct"),
    ("P(fail total)", "p_fail_total", "pct"), ("Не завершено", "p_unresolved", "pct"),
    ("Медиана дней s1", "median_days_s1", "num"), ("p90 дней s1", "p90_days_s1", "num"),
    ("Сделок на s1", "trades_per_s1", "num"), ("Стартов (полный горизонт)", "n_starts_full", "int"),
]


def _ci(b: dict, key: str) -> str:
    c = b.get(key)
    if not c:
        return ""
    return f"bootstrap 95%: {c['lo'] * 100:.1f}–{c['hi'] * 100:.1f}%"


def challenge_section(m: dict, boot: dict) -> str:
    tiles = [
        tile("P(pass both)", m.get("p_pass_both"), "pct", _ci(boot, "p_pass_both")),
        tile("P(pass stage1)", m.get("p_pass_s1"), "pct", _ci(boot, "p_pass_s1")),
        tile("P(fail по дневному)", m.get("p_fail_daily"), "pct", _ci(boot, "p_fail_daily")),
        tile("Не завершено за H", m.get("p_unresolved"), "pct"),
        tile("Медиана дней до stage1", m.get("median_days_s1"), "num"),
        tile("EV челленджа", m.get("ev_usd"), "usd", "P(both) × E[выплат] − цена"),
    ]
    return f'<div class="tiles">{"".join(tiles)}</div>'


def build_report(*, run_id: int, spec, tradeset: TradeSet, challenge: ChallengeResult, classic: dict, stress: dict,
                 bootstrap: dict, baseline: dict | None, risk: RiskParams, cfg: ChallengeConfig, challenge_type: str,
                 size: float, dataset_hash: str, out_path: Path) -> Path:
    from quant.backtest.engine import sample_equity_curves

    m = challenge.metrics
    sections: list[tuple[str, str]] = []
    warn = []
    if m.get("n_starts_full", 0) < 60:
        warn.append(f"Мало стартов с полным горизонтом ({m.get('n_starts_full', 0)}): оценки P(pass) неустойчивы.")
    if classic.get("n_trades", 0) < 100:
        warn.append(f"Мало сделок ({classic.get('n_trades', 0)}): выводы предварительные.")
    head = "".join(f'<div class="warn">{w}</div>' for w in warn)
    sections.append(("Итог челленджа", head + challenge_section(m, bootstrap)))

    rows = [[name, m.get(k), (baseline or {}).get(k)] for name, k, _ in KEY_ROWS]
    kinds = ["str"] + [KEY_ROWS[i][2] for i in range(len(KEY_ROWS))]
    tab_rows = []
    for (name, k, kind) in KEY_ROWS:
        tab_rows.append((name, m.get(k), (baseline or {}).get(k), kind))
    body = '<div class="scroll"><table><thead><tr><th>Метрика</th><th>Стратегия</th><th>baseline_random</th></tr></thead><tbody>'
    from quant.reports.html import _fmt
    for name, a, b, kind in tab_rows:
        body += f"<tr><td>{name}</td><td>{_fmt(a, kind)}</td><td>{_fmt(b, kind)}</td></tr>"
    body += "</tbody></table></div>"
    if baseline and baseline.get("bootstrap"):
        bb = baseline["bootstrap"].get("p_pass_both")
        sb = bootstrap.get("p_pass_both")
        if bb and sb:
            sig = sb["lo"] > bb["hi"]
            body += (f'<p class="sub">P(pass both): стратегия {sb["lo"] * 100:.1f}–{sb["hi"] * 100:.1f}%, baseline '
                     f'{bb["lo"] * 100:.1f}–{bb["hi"] * 100:.1f}% → '
                     f'{"значимо выше baseline" if sig else "НЕ значимо выше baseline (стратегия не доказала пользу)"}.</p>')
    sections.append(("Сравнение с baseline_random", body))

    if stress:
        srows = [[name, v.get("p_pass_s1"), v.get("p_pass_both"), v.get("p_fail_daily"), v.get("p_unresolved")]
                 for name, v in stress.items()]
        srows.insert(0, ["базовый", m.get("p_pass_s1"), m.get("p_pass_both"), m.get("p_fail_daily"), m.get("p_unresolved")])
        sections.append(("Стресс-сценарии", table(["Сценарий", "P(s1)", "P(both)", "P(fail daily)", "Не завершено"],
                                                  srows, ["str", "pct", "pct", "pct", "pct"])))

    arr = to_sim_arrays(tradeset)
    curves = sample_equity_curves(arr, cfg, challenge, challenge_type=challenge_type, size=size, risk=risk,
                                  horizon_days=challenge.horizons_days[0])
    if curves:
        stage = cfg.stage(challenge_type, cfg.stage_names(challenge_type)[0])
        refs = [(size * (1 + (stage.target_pct or 0)), "цель"), (size * (1 - stage.max_loss_pct), "общий floor")]
        series = [{"x": c["x"], "y": c["y"], "color": STATUS_COLOR[c["status"]], "opacity": 0.8,
                   "title": f"старт {c['start']:%Y-%m-%d}: {STATUS_NAME[c['status']]}"} for c in curves]
        legend = ('<div class="legend"><span><i style="background:var(--good)"></i>прошёл</span>'
                  '<span><i style="background:var(--critical)"></i>провал</span>'
                  '<span><i style="background:var(--neutral)"></i>не завершён</span></div>')
        sections.append(("Баланс stage 1 для выборки стартов (дни от старта)",
                         legend + svg_lines(series, y_ref=refs, height=300)))

    res = challenge.res
    if len(res):
        passed = res[:, 0, 0] == S.ST_PASS
        days = (res[passed, 0, 2] - res[passed, 0, 1]) / MIN_PER_DAY
        labels, counts = histogram(days, bins=min(20, max(5, int(len(days) ** 0.5))))
        fails = res[res[:, 0, 0] == S.ST_FAIL, 0, 3]
        fl = [REASON_NAME[r] for r in (S.R_DAILY, S.R_TOTAL, S.R_LIQ)]
        fv = [float((fails == r).sum()) for r in (S.R_DAILY, S.R_TOTAL, S.R_LIQ)]
        if len(days):
            body_days = svg_bars(labels, counts) + table(["от, дней", "стартов"], list(zip(labels, counts)), ["str", "int"])
        else:
            body_days = '<p class="empty">Ни один старт не прошёл stage 1 за горизонт.</p>'
        sections.append(("Дней до прохождения stage 1", body_days))
        if sum(fv):
            body_fail = svg_bars(fl, fv, height=160) + table(["причина", "стартов"], list(zip(fl, fv)), ["str", "int"])
        else:
            body_fail = '<p class="empty">Провалов stage 1 нет (все старты прошли или не завершились за горизонт).</p>'
        sections.append(("Причины провала stage 1", body_fail))

    crow = [[k, v] for k, v in classic.items()]
    sections.append(("Классические метрики (1R на сделку)", table(["метрика", "значение"], crow, ["str", "num"])))
    g = by_group(tradeset.trades)
    if g.height:
        sections.append(("По группам и сторонам", table(g.columns, g.rows(), ["str", "num", "int", "pct", "num", "pct", "pct"])))

    q_rows = []
    for sym in spec.symbols:
        try:
            cov = store.coverage(sym)
        except Exception:
            continue
        q_rows.append([sym, cov.get("first_ts"), cov.get("last_ts"), cov.get("rows"), cov.get("halted_min"),
                       cov.get("zero_vol_min"), cov.get("gaps_min")])
    if q_rows:
        sections.append(("Качество данных", table(["символ", "с", "по", "минут", "halted", "zero_vol", "гэпы, мин"],
                                                  q_rows, ["str", "str", "str", "int", "int", "int", "int"])))
    repro = table(["поле", "значение"], [
        ["run_id", run_id], ["стратегия", spec.strategy], ["переопределения", str(spec.overrides)],
        ["символы", ", ".join(spec.symbols)], ["период", f"{spec.start:%Y-%m-%d} … {spec.end:%Y-%m-%d}"],
        ["челлендж", f"{challenge_type}, ${size:,.0f}, H={challenge.horizons_days[0]:.0f} дн."],
        ["risk", str(risk.__dict__)], ["опции правил", str(cfg.options.model_dump())],
        ["dataset_hash", dataset_hash[:16]], ["code_version", code_version()],
    ], ["str", "str"])
    sections.append(("Воспроизводимость", repro))
    return render(title=f"Бэктест #{run_id}: {spec.strategy}",
                  subtitle=f"{', '.join(spec.symbols)} · {spec.start:%Y-%m-%d} … {spec.end:%Y-%m-%d}",
                  sections=sections, out_path=out_path)
