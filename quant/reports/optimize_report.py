"""Отчёт walk-forward: окна, выбранные параметры, OOS, чувствительность, DSR/PBO."""

from __future__ import annotations

from pathlib import Path

import polars as pl

from quant.optimize.walkforward import WFConfig, WFResult
from quant.reports.html import _fmt, render, svg_bars, table, tile
from quant.timeutil import from_min


def build_wf_report(*, run_id: int, strategy: str, symbols: list[str], res: WFResult, wf: WFConfig,
                    out_path: Path) -> Path:
    m = res.oos_metrics
    c = res.oos_classic
    sections = []
    warn = []
    if (res.pbo.get("pbo") or 0) > 0.5:
        warn.append(f"PBO = {res.pbo['pbo']:.2f}: высокая вероятность переподгонки — лучший in-sample вариант "
                    "чаще проигрывает медиане out-of-sample.")
    if res.dsr.get("dsr") is not None and res.dsr.get("dsr") == res.dsr.get("dsr") and res.dsr["dsr"] < 0.95:
        warn.append(f"DSR = {res.dsr['dsr']:.2f} < 0.95: с учётом числа испытаний Sharpe не значим.")
    head = "".join(f'<div class="warn">{w}</div>' for w in warn)
    tiles = [
        tile("OOS P(pass stage1)", m.get("p_pass_s1"), "pct"),
        tile("OOS P(pass both)", m.get("p_pass_both"), "pct"),
        tile("OOS P(fail daily)", m.get("p_fail_daily"), "pct"),
        tile("OOS winrate", c.get("winrate"), "pct", f"сделок {c.get('n_trades', 0)}"),
        tile("DSR", res.dsr.get("dsr"), "num", f"испытаний {res.dsr.get('n_trials')}"),
        tile("PBO", res.pbo.get("pbo"), "num", "< 0.5 — хорошо"),
    ]
    sections.append(("Итог out-of-sample", head + f'<div class="tiles">{"".join(tiles)}</div>'))
    rows = []
    for w, ci in zip(res.windows, res.chosen):
        sub = res.train_table.filter((pl.col("window") == w.idx) & (pl.col("combo") == ci)) if ci >= 0 else pl.DataFrame()
        tr = sub.row(0, named=True) if sub.height else {}
        rows.append([w.label, str(res.combos[ci]["point"]) if ci >= 0 else "—", tr.get("objective"),
                     tr.get("p_pass_s1"), tr.get("p_fail_daily_max"), tr.get("n_trades")])
    sections.append(("Окна walk-forward (выбор на train)",
                     table(["окно train → test", "параметры", "цель (train)", "P(s1) train", "P(fail daily) max", "сделок"],
                           rows, ["str", "str", "pct", "pct", "pct", "int"])))
    final = res.combos[res.final_combo]["point"] if res.final_combo >= 0 else {}
    sens = res.extra.get("sensitivity", {})
    sections.append(("Финальные параметры и чувствительность",
                     table(["поле", "значение"], [["параметры", str(final)],
                                                  ["цель (весь период без hold-out)", _fmt(sens.get("objective"), "pct")],
                                                  ["соседи: min", _fmt(sens.get("neighbors_min"), "pct")],
                                                  ["соседи: max", _fmt(sens.get("neighbors_max"), "pct")],
                                                  ["hold-out с", f"{from_min(res.holdout_start):%Y-%m-%d} (не тронут)"]],
                           ["str", "str"])))
    fo = res.extra.get("full_objective", [])
    if fo:
        labels = [str(i) for i in range(len(fo))]
        vals = [max(0.0, v) if v == v and v != float("-inf") else 0.0 for v in fo]
        sections.append(("Цель по вариантам (весь период без hold-out)", svg_bars(labels, vals, fmt="{:.2f}", height=180)))
    sections.append(("Поправка на множественное тестирование",
                     table(["метрика", "значение"], [[k, v] for k, v in {**res.dsr, **{f"pbo_{k}": v for k, v in res.pbo.items()}}.items()],
                           ["str", "num"])))
    sections.append(("OOS-сделки (классика)", table(["метрика", "значение"], [[k, v] for k, v in c.items()], ["str", "num"])))
    return render(title=f"Walk-forward #{run_id}: {strategy}", subtitle=", ".join(symbols), sections=sections,
                  out_path=out_path)
