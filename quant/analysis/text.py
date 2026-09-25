"""Текстовый разбор сетапа (раздел 8.3 ТЗ): 3–6 предложений, шаблонно и детерминированно.

Собирается только из входных данных: факторов стратегии (Strategy.explain) и контекста.
Никаких утверждений, которых нет во входе: нет данных — нет предложения.

context (всё опционально):
    hh_symbol, tf                 — заголовок;
    score_long, score_short       — иначе считается из факторов: 100·Σpoints/Σmax_points;
    is_alt: bool, btc_trend       — тренд BTC (-1/0/1) для альтов;
    is_tradfi: bool, session_open, session_next_change (str) — сессия базового рынка;
    funding_rate, funding_hot     — ставка funding и порог перегрева (дефолт 0.0005);
    signal: {side, entry, sl, tp1, tp2} — активный сигнал.
"""

from __future__ import annotations

import math
from typing import Any, Iterable

from quant.strategies.base import Factor

MAX_SENTENCES = 6
TOP_N = 4
SIDE_RU = {"long": "лонг", "short": "шорт"}
SIDE_GEN = {"long": "лонга", "short": "шорта"}


def _score(factors: Iterable[Factor]) -> float | None:
    fs = [f for f in factors if f.max_points > 0]
    tot = sum(f.max_points for f in fs)
    if not fs or tot <= 0:
        return None
    return 100.0 * sum(f.points for f in fs) / tot


def _num(x: Any) -> float | None:
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def fmt_price(x: float) -> str:
    """Цена без лишних знаков: 142.35, 0.0012345, 64250.5."""
    ax = abs(x)
    if ax >= 1000:
        s = f"{x:.2f}"
    elif ax >= 1:
        s = f"{x:.4f}"
    elif ax == 0:
        return "0"
    else:
        digits = max(4, -int(math.floor(math.log10(ax))) + 4)   # 5 значащих цифр
        s = f"{x:.{digits}f}"
    return s.rstrip("0").rstrip(".") if "." in s else s


def _labels(factors: Iterable[Factor], verdict: str, limit: int = TOP_N) -> list[str]:
    fs = [f for f in factors if f.verdict == verdict]
    # сначала факторы с баллами (по вкладу), затем фильтры/контекст — в исходном порядке
    scored = sorted((f for f in fs if f.max_points > 0), key=lambda f: -(f.points if verdict == "pro" else f.max_points))
    info = [f for f in fs if f.max_points <= 0]
    return [f.label for f in (scored + info)[:limit]]


def _lower_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s and not s[:2].isupper() else s


def _join(labels: list[str]) -> str:
    return ", ".join(_lower_first(x) if i else x for i, x in enumerate(labels))


def describe(factors_long: list[Factor], factors_short: list[Factor], context: dict[str, Any] | None = None) -> list[str]:
    ctx = context or {}
    s_long = _num(ctx.get("score_long"))
    s_long = s_long if s_long is not None else _score(factors_long)
    s_short = _num(ctx.get("score_short"))
    s_short = s_short if s_short is not None else _score(factors_short)
    head = " ".join(str(x) for x in (ctx.get("hh_symbol"), str(ctx.get("tf") or "").upper()) if x)
    head = f"{head}: " if head else ""

    # 1. Итог по скорам
    if s_long is None and s_short is None:
        summary = f"{head}score не рассчитан — недостаточно данных."
        lead, other = "long", "short"
    else:
        sl = s_long if s_long is not None else 0.0
        ss = s_short if s_short is not None else 0.0
        lead, other = ("long", "short") if sl >= ss else ("short", "long")
        tail = "силы сторон близки" if abs(sl - ss) < 10 else f"перевес у {SIDE_GEN[lead]}"
        summary = f"{head}score лонга {sl:.0f}, шорта {ss:.0f} — {tail}."
    fl = {"long": factors_long, "short": factors_short}

    # 2–3. Ведущая сторона: за и против
    pros = _labels(fl[lead], "pro")
    cons = _labels(fl[lead], "con")
    s_pro = (f"За {SIDE_RU[lead]}: {_join(pros)}." if pros
             else f"Факторов за {SIDE_RU[lead]} нет.")
    s_con = (f"Против {SIDE_GEN[lead]}: {_join(cons)}." if cons
             else f"Факторов против {SIDE_GEN[lead]} нет.")

    # 4. Другая сторона — одним предложением
    o_pro = _labels(fl[other], "pro", 3)
    o_con = _labels(fl[other], "con", 3)
    if o_pro or o_con:
        parts = []
        if o_pro:
            parts.append("за — " + _join(o_pro))
        if o_con:
            parts.append("против — " + _join(o_con))
        s_other = f"Для {SIDE_GEN[other]}: " + "; ".join(parts) + "."
    else:
        s_other = None

    # 5. Контекст: BTC для альтов, сессия для TradFi, funding
    cparts: list[str] = []
    btc = _num(ctx.get("btc_trend"))
    if ctx.get("is_alt") and btc is not None:
        cparts.append({1: "тренд BTC восходящий", -1: "тренд BTC нисходящий"}.get(int(btc), "BTC во флэте"))
    if ctx.get("is_tradfi") and ctx.get("session_open") is not None:
        if ctx["session_open"]:
            cparts.append("сессия базового рынка открыта")
        else:
            nxt = ctx.get("session_next_change")
            cparts.append("сессия базового рынка закрыта" + (f" (откроется {nxt})" if nxt else ""))
    rate = _num(ctx.get("funding_rate"))
    if rate is not None:
        hot = _num(ctx.get("funding_hot")) or 0.0005
        if rate >= hot:                 # знак платежа — раздел 2.5 ТЗ: rate > 0 платит лонг
            q = "высокий, лонги платят шортам"
        elif rate <= -hot:
            q = "глубоко отрицательный, шорты платят лонгам"
        else:
            q = "нейтральный"
        cparts.append(f"funding {rate * 100:+.4f}% — {q}")
    s_ctx = (cparts[0][:1].upper() + "; ".join(cparts)[1:] + ".") if cparts else None

    # 6. Активный сигнал
    s_sig = None
    sig = ctx.get("signal")
    if isinstance(sig, dict) and sig.get("side") in SIDE_RU:
        bits = []
        for key, name in (("entry", "вход"), ("sl", "SL"), ("tp1", "TP1"), ("tp2", "TP2")):
            val = _num(sig.get(key))
            if val is not None:
                bits.append(f"{name} {fmt_price(val)}")
        s_sig = f"Активный сигнал: {SIDE_RU[sig['side']]}" + (", " + ", ".join(bits) if bits else "") + "."

    out = [summary, s_pro, s_con]
    for s in (s_other, s_ctx, s_sig):
        if s:
            out.append(s)
    return out[:MAX_SENTENCES]
