"""HTML-отчёт на прогон (раздел 7): самодостаточный файл, inline SVG, тёмная/светлая тема.

Разделы: параметры и воспроизводимость, главные метрики челленджа, сравнение с baseline, стресс,
кривые equity выборки челленджей, дни до прохождения, причины провала, walk-forward,
классические метрики, DSR/PBO, качество данных.
"""

from __future__ import annotations

import html
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

CSS = """
:root{color-scheme:light;--surface:#fcfcfb;--surface-2:#f3f2ef;--text:#0b0b0b;--text-2:#52514e;--muted:#8a8984;
--grid:#e4e3df;--series:#2a78d6;--good:#0ca30c;--critical:#d03b3b;--neutral:#8a8984;--warn:#fab219}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;--surface:#1a1a19;
--surface-2:#242422;--text:#fff;--text-2:#c3c2b7;--muted:#8f8e86;--grid:#383835;--series:#3987e5}}
:root[data-theme="dark"]{color-scheme:dark;--surface:#1a1a19;--surface-2:#242422;--text:#fff;--text-2:#c3c2b7;
--muted:#8f8e86;--grid:#383835;--series:#3987e5}
body{margin:0;background:var(--surface);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",sans-serif}
.viz-root{max-width:1100px;margin:0 auto;padding:24px 16px 64px}
.empty{color:var(--text-2);margin:4px 0 0}
h1{font-size:22px;margin:0 0 4px} h2{font-size:17px;margin:32px 0 8px;border-bottom:1px solid var(--grid);padding-bottom:4px}
.sub{color:var(--text-2);margin:0 0 16px}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px}
.tile{background:var(--surface-2);border-radius:8px;padding:10px 12px}
.tile .k{color:var(--text-2);font-size:12px}.tile .v{font-size:22px;font-weight:600;font-variant-numeric:tabular-nums}
.tile .n{color:var(--muted);font-size:12px}
table{border-collapse:collapse;width:100%;font-variant-numeric:tabular-nums;font-size:13px}
th,td{text-align:right;padding:4px 8px;border-bottom:1px solid var(--grid)} th:first-child,td:first-child{text-align:left}
th{color:var(--text-2);font-weight:500}
.scroll{overflow-x:auto}
svg text{fill:var(--text-2);font-size:11px}
.legend{display:flex;gap:14px;color:var(--text-2);font-size:12px;margin:4px 0}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:4px;vertical-align:-1px}
details{margin-top:6px;color:var(--text-2)} .warn{color:var(--text);background:var(--surface-2);border-left:3px solid var(--warn);padding:8px 10px;margin:6px 0}
code{font-size:12px}
"""


def _fmt(v: Any, kind: str = "num") -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "—"
    if kind == "pct":
        return f"{v * 100:.1f}%"
    if kind == "usd":
        return f"${v:,.0f}"
    if kind == "int":
        return f"{int(v):,}"
    if isinstance(v, float):
        return f"{v:.3f}" if abs(v) < 100 else f"{v:,.1f}"
    return html.escape(str(v))


def tile(label: str, value: Any, kind: str = "pct", note: str = "") -> str:
    return (f'<div class="tile"><div class="k">{html.escape(label)}</div><div class="v">{_fmt(value, kind)}</div>'
            f'<div class="n">{html.escape(note)}</div></div>')


def table(headers: list[str], rows: Iterable[list[Any]], kinds: list[str] | None = None) -> str:
    kinds = kinds or ["str"] + ["num"] * (len(headers) - 1)
    h = "".join(f"<th>{html.escape(x)}</th>" for x in headers)
    body = "".join("<tr>" + "".join(f"<td>{_fmt(v, k)}</td>" for v, k in zip(r, kinds)) + "</tr>" for r in rows)
    return f'<div class="scroll"><table><thead><tr>{h}</tr></thead><tbody>{body}</tbody></table></div>'


def svg_lines(series: list[dict], *, width: int = 1060, height: int = 260, y_ref: list[tuple[float, str]] | None = None,
              x_label: str = "", y_fmt: str = "{:,.0f}") -> str:
    """series: [{x: [...], y: [...], color: css-var, title: str, opacity}]"""
    xs = [x for s in series for x in s["x"]]
    ys = [y for s in series for y in s["y"]] + [r[0] for r in (y_ref or [])]
    if not xs:
        return ""
    x0, x1 = min(xs), max(xs) or 1
    y0, y1 = min(ys), max(ys)
    if y1 == y0:
        y1 = y0 + 1
    pad_l, pad_b, pad_t = 56, 22, 8
    w, h = width - pad_l - 8, height - pad_b - pad_t
    sx = lambda v: pad_l + (v - x0) / ((x1 - x0) or 1) * w  # noqa: E731
    sy = lambda v: pad_t + (1 - (v - y0) / (y1 - y0)) * h  # noqa: E731
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">']
    for t in np.linspace(y0, y1, 5):
        parts.append(f'<line x1="{pad_l}" x2="{width - 8}" y1="{sy(t):.1f}" y2="{sy(t):.1f}" stroke="var(--grid)"/>'
                     f'<text x="{pad_l - 6}" y="{sy(t) + 4:.1f}" text-anchor="end">{y_fmt.format(t)}</text>')
    for t in np.linspace(x0, x1, 6):
        parts.append(f'<text x="{sx(t):.1f}" y="{height - 6}" text-anchor="middle">{t:.0f}</text>')
    for v, name in (y_ref or []):
        parts.append(f'<line x1="{pad_l}" x2="{width - 8}" y1="{sy(v):.1f}" y2="{sy(v):.1f}" stroke="var(--muted)" '
                     f'stroke-dasharray="4 4"/><text x="{width - 10}" y="{sy(v) - 4:.1f}" text-anchor="end">{html.escape(name)}</text>')
    for s in series:
        pts = " ".join(f"{sx(a):.1f},{sy(b):.1f}" for a, b in zip(s["x"], s["y"]))
        parts.append(f'<polyline fill="none" stroke="{s.get("color", "var(--series)")}" stroke-width="{s.get("width", 1.5)}" '
                     f'stroke-opacity="{s.get("opacity", 1)}" points="{pts}"><title>{html.escape(s.get("title", ""))}</title></polyline>')
    if x_label:
        parts.append(f'<text x="{pad_l + w / 2:.0f}" y="{height - 6}" text-anchor="middle" dy="0">{html.escape(x_label)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def svg_bars(labels: list[str], values: list[float], *, width: int = 1060, height: int = 200, fmt: str = "{:.0f}",
             color: str = "var(--series)", colors: list[str] | None = None) -> str:
    if not values:
        return ""
    vmax = max(values) or 1
    n = len(values)
    pad_l, pad_b, pad_t = 8, 22, 16
    w, h = width - 2 * pad_l, height - pad_b - pad_t
    bw = w / n
    parts = [f'<svg viewBox="0 0 {width} {height}" width="100%" role="img">',
             f'<line x1="{pad_l}" x2="{width - pad_l}" y1="{pad_t + h}" y2="{pad_t + h}" stroke="var(--grid)"/>']
    for i, (lab, v) in enumerate(zip(labels, values)):
        bh = v / vmax * h
        x = pad_l + i * bw + 1
        c = colors[i] if colors else color
        parts.append(f'<rect x="{x:.1f}" y="{pad_t + h - bh:.1f}" width="{max(bw - 2, 1):.1f}" height="{bh:.1f}" rx="2" fill="{c}">'
                     f'<title>{html.escape(lab)}: {fmt.format(v)}</title></rect>')
        if n <= 24:
            parts.append(f'<text x="{x + bw / 2:.1f}" y="{height - 6}" text-anchor="middle">{html.escape(lab)}</text>')
            if v > 0:
                parts.append(f'<text x="{x + bw / 2:.1f}" y="{pad_t + h - bh - 3:.1f}" text-anchor="middle">{fmt.format(v)}</text>')
    parts.append("</svg>")
    return "".join(parts)


def histogram(values: np.ndarray, bins: int = 20) -> tuple[list[str], list[float]]:
    if len(values) == 0:
        return [], []
    cnt, edges = np.histogram(values, bins=bins)
    return [f"{edges[i]:.0f}" for i in range(len(cnt))], cnt.astype(float).tolist()


def render(*, title: str, subtitle: str, sections: list[tuple[str, str]], out_path: Path) -> Path:
    body = "".join(f"<h2>{html.escape(h)}</h2>{content}" for h, content in sections)
    doc = (f'<!doctype html><html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
           f'<title>{html.escape(title)}</title><style>{CSS}</style></head><body><div class="viz-root">'
           f'<h1>{html.escape(title)}</h1><p class="sub">{html.escape(subtitle)}</p>{body}'
           f'<p class="sub" style="margin-top:40px">Сгенерировано {datetime.now(timezone.utc):%Y-%m-%d %H:%M} UTC</p>'
           f'</div></body></html>')
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    return out_path
