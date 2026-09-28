"""feature_rules — вход по правилам над признаками бара (проверка гипотез из анализа лучших входов).

Правила — списки [признак, оператор, порог] для каждой стороны (все условия через И; пустой список —
сторона выключена). Признаки — quant.research.features (тот же расчёт, что при разметке истории).
Сигнал — на первом баре, где правила выполнены, если в предыдущие rearm_bars баров они не выполнялись
(один вход на эпизод; конечная память — расчёт на окне совпадает с полным).
Выходы: стоп, цель tp_r·R, удержание max_hold_min. Стоп (sl_mode): atr — sl_atr·ATR от close; d1_atr — sl_atr
дневных ATR; safezone — стоп SafeZone Элдера; bar — за экстремум сигнального бара ± sl_buffer_atr·ATR (ложный
пробой). Для safezone/bar расстояние ограничено [sl_min_atr, sl_max_atr]·ATR. Частичная фиксация (Дуглас):
tp2_r задан — доля tp1_frac закрывается на tp_r, остаток на tp2_r; be_after_tp1 — стоп остатка в безубыток после
первой цели.

entry_mode = random — контроль: те же выходы, случайные входы (частота random_signals_per_day,
доля шортов random_short_share).

Фильтр широты рынка (breadth_min_frac > 0): сигнал стороны берётся, только если в этот же час правила
breadth_rules_<side> (по умолчанию — rules_<side>) выполнены у доли рынка >= breadth_min_frac. Ряд широты
по всем инструментам считает движок (quant.backtest.breadth) и передаёт в meta["breadth"]; без ряда фильтр
закрыт — сигналов нет. Фильтр применяется после правила «один вход на эпизод», как в анализе, где он найден.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from quant.indicators import core
from quant.schemas import TS
from quant.strategies.base import Factor, Side, Strategy
from quant.strategies.baseline_random import hash_uniform
from quant.strategies.common import NullSafeSignals, assemble, bar_flags, deep_merge, session_ok
from quant.timeutil import tf_minutes

DEFAULTS: dict[str, Any] = {
    "tf": "1h",
    "htf": "4h",
    "rules_long": [],
    "rules_short": [],
    "rearm_bars": 24,
    "sl_atr": 1.5,
    "sl_mode": "atr",
    "sl_min_atr": 0.5,
    "sl_max_atr": 3.0,
    "sl_buffer_atr": 0.2,
    "tp_r": 2.0,
    "tp2_r": None,
    "tp1_frac": 1.0,
    "be_after_tp1": None,       # None — как в execution конфига
    "max_hold_min": 2880,
    "valid_min": 60,
    "entry_type": "market",
    "sl_trigger": "last",
    "entry_mode": "setup",
    "random_signals_per_day": 0.2,
    "random_short_share": 0.5,
    "seed": 42,
    "breadth_min_frac": 0.0,
    "breadth_rules_long": None,
    "breadth_rules_short": None,
    "filters": {"tradfi_off_session_signals": False},
}


SL_MODES = ("atr", "safezone", "bar", "d1_atr")


def stop_distance(p: dict[str, Any], f: dict[str, np.ndarray], side: str) -> np.ndarray:
    """Расстояние от close до стопа (в цене) по sl_mode; NaN — стоп не определён (сигнала не будет)."""
    c, atr = f["_close"], f["_atr"]
    mode = p["sl_mode"]
    if mode == "atr":
        return float(p["sl_atr"]) * atr
    if mode == "d1_atr":                           # дневной масштаб: sl_atr дневных ATR (без ограничения в 1h-ATR)
        return float(p["sl_atr"]) * f["d1_atr_pct"] / 100.0 * c
    with np.errstate(invalid="ignore"):
        if mode == "safezone":
            d = f[f"sz_{side}_atr"] * atr
        else:                                      # bar: за экстремум сигнального бара
            buf = float(p["sl_buffer_atr"]) * atr
            d = (c - f["_low"] + buf) if side == "long" else (f["_high"] - c + buf)
        return np.clip(d, float(p["sl_min_atr"]) * atr, float(p["sl_max_atr"]) * atr)


def breadth_rules(params: dict[str, Any], side: str) -> list:
    """Правила, по которым считается широта для стороны (по умолчанию — правила входа)."""
    r = params.get(f"breadth_rules_{side}")
    return list(params.get(f"rules_{side}") or []) if r is None else list(r)


def breadth_gate(meta: dict[str, Any], close_ms: np.ndarray, side: str) -> np.ndarray:
    """Доля рынка с выполненными правилами на закрытии этого же бара (NaN — ряда нет или нет такого часа)."""
    b = meta.get("breadth")
    out = np.full(len(close_ms), np.nan)
    if b is None or b.is_empty():
        return out
    t = b["close_ts"].dt.epoch("ms").to_numpy().astype(np.int64)
    v = b[f"frac_{side}"].to_numpy().astype(np.float64)
    idx = np.searchsorted(t, close_ms)
    ok = (idx < len(t)) & (t[np.minimum(idx, len(t) - 1)] == close_ms)
    out[ok] = v[idx[ok]]
    return out


class FeatureRules(NullSafeSignals, Strategy):
    name = "feature_rules"

    def __init__(self, params: dict[str, Any] | None = None):
        from quant.research.features import check_rules      # лениво: features импортирует quant.strategies.common

        super().__init__(deep_merge(DEFAULTS, params))
        for side in ("long", "short"):
            check_rules(self.params[f"rules_{side}"], "feature_rules")
            check_rules(breadth_rules(self.params, side), "feature_rules.breadth")
        if self.params["entry_mode"] not in ("setup", "random"):
            raise ValueError(f"feature_rules: entry_mode {self.params['entry_mode']!r}")
        if self.params["sl_mode"] not in SL_MODES:
            raise ValueError(f"feature_rules: sl_mode {self.params['sl_mode']!r}, есть {SL_MODES}")

    def warmup_bars(self) -> int:
        """Окно баров ТФ для on_bar: EMA200 1h сходится за ~25×200 баров, остальное — за 600."""
        from quant.research.features import SLOW_FEATURES

        used = {r[0] for side in ("long", "short") for r in self.params[f"rules_{side}"]}
        slow = SLOW_FEATURES & used
        return max(5000 if slow else 600, 168 + int(self.params["rearm_bars"]))

    def compute(self, bars_tf: pl.DataFrame, *, hh_symbol: str, tf: str, htf: pl.DataFrame | None = None,
                bars_1m: pl.DataFrame | None = None, meta: dict[str, Any] | None = None) -> pl.DataFrame:
        from quant.research.features import FEATURES, bar_features, rules_mask

        p = self.params
        meta = meta or {}
        df, live, f = bar_features(bars_tf, tf=tf, htf_tf=p["htf"], htf=htf, bars_1m=bars_1m, meta=meta)
        if df.is_empty():
            return self._empty()
        _, blocked_all = bar_flags(df)
        blocked = blocked_all[live]
        c, atr, close_ms = f["_close"], f["_atr"], f["_close_ms"].astype(np.int64)
        n = len(c)
        with np.errstate(invalid="ignore"):
            ready = ~np.isnan(atr) & (atr > 0)
            base_ok = ready & ~blocked & session_ok(close_ms, meta.get("instrument"), p.get("filters") or {})
            conds, hits, breadth = {}, {}, {}
            min_frac = float(p.get("breadth_min_frac") or 0.0)
            for side in ("long", "short"):
                hit = rules_mask(f, p[f"rules_{side}"], n)
                hits[side] = hit
                cond = base_ok & hit
                rb = int(p["rearm_bars"])
                prev = core.shift(core.highest(cond.astype(np.float64), rb), 1) > 0 if rb > 0 else np.zeros(n, bool)
                conds[side] = cond & ~prev
                breadth[side] = breadth_gate(meta, close_ms, side)
                if min_frac > 0:
                    conds[side] &= breadth[side] >= min_frac          # NaN (нет ряда) → False
            if p["entry_mode"] == "random":
                prob = min(1.0, float(p["random_signals_per_day"]) * tf_minutes(tf) / 1440.0)
                u_fire = hash_uniform(int(p["seed"]), hh_symbol, close_ms, 0)
                u_side = hash_uniform(int(p["seed"]), hh_symbol, close_ms, 1)
                fire = base_ok & (u_fire < prob)
                is_long = u_side < 1.0 - float(p["random_short_share"])
                conds = {"long": fire & is_long, "short": fire & ~is_long}
                if min_frac > 0:
                    conds = {s: conds[s] & (breadth[s] >= min_frac) for s in conds}
            cols: dict[str, np.ndarray] = {"close": c}
            for side in ("long", "short"):
                cols[f"score_{side}"] = np.where(ready, 100.0 * hits[side], np.nan)
            tp2_r = p.get("tp2_r")
            for side, sgn in (("long", 1.0), ("short", -1.0)):
                dist = stop_distance(p, f, side)
                conds[side] &= ~np.isnan(dist) & (dist > 0)
                cols[f"entry_{side}"] = np.where(ready, c, np.nan)
                cols[f"sl_{side}"] = c - sgn * dist
                cols[f"tp1_{side}"] = c + sgn * float(p["tp_r"]) * dist
                cols[f"tp2_{side}"] = c + sgn * float(tp2_r) * dist if tp2_r else np.full(n, np.nan)
            cols["signal_long"], cols["signal_short"] = conds["long"], conds["short"]
            for side in ("long", "short"):
                cols[f"c_rules_{side}"] = np.where(ready, 100.0 * hits[side], np.nan)
            cols.update({
                "rsi14": f["rsi14"], "adx": f["adx"], "atr_pct": f["atr_pct"],
                "vwap_dist_pct": f["vwap_dist_atr"] * f["atr_pct"], "htf_trend": f["htf_trend"].astype(np.int8),
                "cvd_trend": np.sign(np.nan_to_num(f["cvd_ratio"])).astype(np.int8), "atr": atr,
                "breadth_long": breadth["long"], "breadth_short": breadth["short"],
                # выполнены ли на этом символе правила широты — live-сервис считает по ним долю рынка за час
                "breadth_hit_long": ready & rules_mask(f, breadth_rules(p, "long"), n),
                "breadth_hit_short": ready & rules_mask(f, breadth_rules(p, "short"), n),
            })
            cols.update({k: f[k] for k in FEATURES if k not in cols})
        return assemble(df, live, cols)

    def _empty(self) -> pl.DataFrame:
        schema: dict[str, Any] = {"ts": TS, "close_ts": TS, "halted": pl.Boolean, "close": pl.Float64,
                                  "score_long": pl.Float64, "score_short": pl.Float64,
                                  "signal_long": pl.Boolean, "signal_short": pl.Boolean}
        for side in ("long", "short"):
            for k in ("entry", "sl", "tp1", "tp2"):
                schema[f"{k}_{side}"] = pl.Float64
        schema.update({"rsi14": pl.Float64, "adx": pl.Float64, "atr_pct": pl.Float64, "vwap_dist_pct": pl.Float64,
                       "htf_trend": pl.Int8, "cvd_trend": pl.Int8})
        return pl.DataFrame(schema=schema)

    def explain(self, row: dict[str, Any], side: Side) -> list[Factor]:
        from quant.research.features import OPS

        out = []
        for feat, op, val in self.params[f"rules_{side}"]:
            v = row.get(feat)
            ok = v is not None and v == v and OPS[op](float(v), float(val))
            out.append(Factor(feat, f"{feat} {op} {val}", None if v is None else float(v), 1.0 if ok else 0.0, 1.0,
                              "pro" if ok else "con"))
        min_frac = float(self.params.get("breadth_min_frac") or 0.0)
        if min_frac > 0:
            b = row.get(f"breadth_{side}")
            ok = b is not None and b == b and b >= min_frac
            out.append(Factor("breadth", f"Широта рынка >= {min_frac:.0%}", None if b is None else float(b),
                              1.0 if ok else 0.0, 1.0, "pro" if ok else "con"))
        return out
