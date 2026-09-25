"""Жизненный цикл сигнала (раздел 8.2): new → active → (triggered | expired | invalidated) → (tp | sl | timeout).

Исход отслеживается автоматически по рыночным данным, даже если сделка не взята (для контроля
калибровки). Логика — как в модели исполнения бэктеста: market-вход по open следующего бара, limit —
только при прохождении цены сквозь уровень, SL раньше TP в одном баре, триггер по last или mark,
бар входа тоже проверяется на выход.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

OPEN_STATES = ("new", "active", "triggered")
FINAL_STATES = ("expired", "invalidated", "tp", "sl", "timeout")


@dataclass
class Transition:
    to: str
    detail: dict


def _hi_lo(bar: dict, trigger: str) -> tuple[float, float]:
    if trigger == "mark" and bar.get("m_high") is not None:
        return float(bar["m_high"]), float(bar["m_low"])
    return float(bar["high"]), float(bar["low"])


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def step(sig, bar: dict) -> list[Transition]:  # noqa: ANN001 — LiveSignal или совместимый объект
    """Продвинуть сигнал одним закрытым 1m-баром. Меняет поля sig (status, fill_price, ...)."""
    out: list[Transition] = []
    ts = datetime.fromtimestamp(int(bar["ts"]) / 1000, tz=timezone.utc)
    side = 1 if sig.side == "long" else -1
    hi, lo = _hi_lo(bar, sig.sl_trigger)

    def go(to: str, **detail) -> None:
        out.append(Transition(to, detail))
        sig.status = to

    if sig.status == "new":
        go("active")
    if sig.status == "active":
        if ts >= _aware(sig.expires_at):
            go("expired")
            return out
        if sig.entry_type == "market":
            fill = float(bar["open"])
            beyond = (fill <= sig.sl or fill >= sig.tp1) if side > 0 else (fill >= sig.sl or fill <= sig.tp1)
            if beyond:
                go("invalidated", open=fill)
                return out
        else:
            touched = (float(bar["low"]) < sig.entry) if side > 0 else (float(bar["high"]) > sig.entry)
            if not touched:
                missed = hi >= sig.tp1 if side > 0 else lo <= sig.tp1
                if missed:
                    go("invalidated", high=hi, low=lo)
                return out
            fill = sig.entry
        sig.fill_price = fill
        sig.triggered_at = ts
        go("triggered", fill_price=fill)
    if sig.status == "triggered":
        entry = sig.fill_price or sig.entry
        risk = abs(entry - sig.sl)
        sl_hit = lo <= sig.sl if side > 0 else hi >= sig.sl
        tp_hit = hi >= sig.tp1 if side > 0 else lo <= sig.tp1
        if sl_hit:
            go("sl", outcome_r=-1.0, ambiguous=bool(tp_hit))
        elif tp_hit:
            go("tp", outcome_r=abs(sig.tp1 - entry) / risk if risk else None)
        elif sig.hold_until is not None and ts >= _aware(sig.hold_until):
            close = float(bar["close"])
            go("timeout", outcome_r=side * (close - entry) / risk if risk else None)
    return out
