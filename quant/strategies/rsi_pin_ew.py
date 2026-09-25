"""rsi_pin_ew — вход по тренду на откате: RSI + пинбар + волна Эллиотта (гипотеза для скрининга).

Лонг, когда выполнены все включённые условия (шорт — зеркально):
- trend  (use_trend) — HTF-тренд вверх: close HTF > EMA(ema_htf_len) и структура HTF не LH/LL;
- rsi    (use_rsi)   — за последние rsi_lookback баров RSI опускался до rsi_long_max или ниже,
                       и на текущем баре RSI растёт (разворот отката);
- pinbar (use_pin)   — бычий пинбар за последние pin_lookback баров: нижняя тень ≥ pin_wick·диапазона,
                       тело ≤ pin_body·диапазона, диапазон ≥ pin_min_atr·ATR;
- elliott (ew_mode)  — формализация по подтверждённым свингам (зигзаг из pivothigh/pivotlow ew_left/ew_right,
                       подряд идущие свинги одного типа сливаются в более экстремальный):
    w2 — вход в волну 3: последние свинги L0 → H1, волна 1 = H1 − L0 ≥ w1_min_atr·ATR; low бара
         откатывает волну 1 на w2_fib[0]..w2_fib[1], и минимум после H1 выше L0 (волна 2 не глубже
         начала волны 1);
    w4 — вход в волну 5: свинги L0 → H1 → L2 → H3, L2 > L0, H3 > H1, волна 3 ≥ w3_min_ratio·волны 1;
         low бара откатывает волну 3 на w4_fib, и минимум после H3 выше H1 (волна 4 не заходит
         в территорию волны 1);
    both — любое из двух; off — условие не проверяется.
Стоп — за минимум отката (минимум пинбара и минимум после последнего свинга) с запасом sl_buf_atr·ATR,
не ближе sl_min_atr·ATR; если стоп дальше sl_max_atr·ATR — сигнала нет. TP1/TP2 — в R от стопа.

entry_mode = random — контроль: те же уровни стопа и тейков, но входы случайные (хэш seed/символ/время,
random_signals_per_day, доля шортов random_short_share). Так видно, дают ли что-то сами условия входа,
а не выходы и перекос по сторонам.

Look-ahead: только закрытые бары; свинги известны на баре подтверждения; HTF — закрытые бары HTF.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl
from numba import njit

from quant.indicators import core
from quant.indicators.pivots import pivothigh, pivotlow
from quant.schemas import TS
from quant.strategies.base import Factor, Side, Strategy
from quant.strategies.baseline_random import hash_uniform
from quant.strategies.common import (
    NullSafeSignals, assemble, bar_flags, deep_merge, edge, htf_block, htf_frame, indicator_block, ms, prepare_bars,
    prop_boundary_min, session_ok,
)
from quant.timeutil import tf_minutes

FACTORS = ("trend", "rsi", "pinbar", "elliott")
EW_MODES = ("off", "w2", "w4", "both")

DEFAULTS: dict[str, Any] = {
    "tf": "1h",
    "htf": "4h",
    "ema_htf_len": 200,
    "htf_swing_left": 3,
    "htf_swing_right": 3,
    "atr_len": 14,
    "rsi_len": 14,
    "vwap_anchor": "utc_day",
    # условия входа
    "use_trend": True,
    "use_rsi": True,
    "rsi_long_max": 40,
    "rsi_short_min": 60,
    "rsi_lookback": 5,
    "use_pin": True,
    "pin_wick": 0.6,
    "pin_body": 0.35,
    "pin_min_atr": 0.5,
    "pin_lookback": 1,
    "ew_mode": "both",
    "ew_left": 5,
    "ew_right": 2,
    "w1_min_atr": 2.0,
    "w2_fib": [0.382, 0.786],
    "w4_fib": [0.236, 0.5],
    "w3_min_ratio": 1.0,
    "edge_trigger": True,
    # план сделки
    "sl_buf_atr": 0.2,
    "sl_min_atr": 0.5,
    "sl_max_atr": 3.0,
    "tp1_r": 1.0,
    "tp2_r": 2.0,
    "tp1_frac": 0.5,
    "max_hold_min": 2880,
    "valid_min": 60,
    "entry_type": "market",
    "sl_trigger": "last",
    # контроль со случайными входами
    "entry_mode": "setup",
    "random_signals_per_day": 0.2,
    "random_short_share": 0.5,
    "seed": 42,
    "filters": {"tradfi_off_session_signals": False},
}


@njit(cache=True)
def _elliott_nb(high, low, ph_idx, pl_idx, atr, w1_min_atr, w2_lo, w2_hi, w4_lo, w4_hi, w3_min_ratio):
    """Зигзаг по подтверждённым свингам и сетапы волн 2→3 и 4→5 на каждом баре.

    Возвращает (w2_long, w4_long, w2_short, w4_short, ext_long, ext_short): ext_* — экстремум отката
    после последнего свинга (включая текущий бар) — опора стопа.
    """
    n = len(high)
    w2_long = np.zeros(n, dtype=np.bool_)
    w4_long = np.zeros(n, dtype=np.bool_)
    w2_short = np.zeros(n, dtype=np.bool_)
    w4_short = np.zeros(n, dtype=np.bool_)
    ext_long = np.full(n, np.nan)
    ext_short = np.full(n, np.nan)
    pv = np.zeros(4)                       # последние 4 свинга: цена, тип (+1 high / -1 low), индекс
    pt = np.zeros(4, dtype=np.int64)
    pi = np.zeros(4, dtype=np.int64)
    k = 0
    run_min = np.inf                       # минимум после последнего swing high (для лонга)
    run_max = -np.inf                      # максимум после последнего swing low (для шорта)
    for i in range(n):
        # свинги, подтверждённые на баре i (по возрастанию индекса пивота)
        a, b = ph_idx[i], pl_idx[i]
        for step in range(2):
            if a >= 0 and (b < 0 or a <= b):
                p, typ, price = a, 1, high[a]
                a = -1
            elif b >= 0:
                p, typ, price = b, -1, low[b]
                b = -1
            else:
                break
            if k > 0 and pt[k - 1] == typ:
                better = price > pv[k - 1] if typ == 1 else price < pv[k - 1]
                if not better:
                    continue
                pv[k - 1] = price
                pi[k - 1] = p
            else:
                if k == 4:
                    for j in range(3):
                        pv[j] = pv[j + 1]
                        pt[j] = pt[j + 1]
                        pi[j] = pi[j + 1]
                    k = 3
                pv[k] = price
                pt[k] = typ
                pi[k] = p
                k += 1
            # экстремум отката — заново от нового последнего свинга до текущего бара
            run_min = np.inf
            run_max = -np.inf
            for j in range(pi[k - 1] + 1, i):
                if low[j] < run_min:
                    run_min = low[j]
                if high[j] > run_max:
                    run_max = high[j]
        if low[i] < run_min:
            run_min = low[i]
        if high[i] > run_max:
            run_max = high[i]
        if k < 2 or np.isnan(atr[i]) or atr[i] <= 0:
            continue
        last = k - 1
        if pt[last] == 1:                  # последний свинг — вершина: возможен откат волны 2 или 4 вверх
            h1, l0 = pv[last], pv[last - 1]
            w1 = h1 - l0
            if w1 >= w1_min_atr * atr[i] and run_min > l0:
                r = (h1 - low[i]) / w1
                if w2_lo <= r <= w2_hi:
                    w2_long[i] = True
            if k == 4:
                l0, h1, l2, h3 = pv[0], pv[1], pv[2], pv[3]
                wave1, wave3 = h1 - l0, h3 - l2
                if (l2 > l0 and h3 > h1 and wave1 >= w1_min_atr * atr[i] and wave3 >= w3_min_ratio * wave1
                        and run_min > h1):
                    r = (h3 - low[i]) / wave3
                    if w4_lo <= r <= w4_hi:
                        w4_long[i] = True
            if w2_long[i] or w4_long[i]:
                ext_long[i] = run_min
        else:                              # последний свинг — впадина: откат вверх в нисходящем движении
            l1, h0 = pv[last], pv[last - 1]
            w1 = h0 - l1
            if w1 >= w1_min_atr * atr[i] and run_max < h0:
                r = (high[i] - l1) / w1
                if w2_lo <= r <= w2_hi:
                    w2_short[i] = True
            if k == 4:
                h0, l1, h2, l3 = pv[0], pv[1], pv[2], pv[3]
                wave1, wave3 = h0 - l1, h2 - l3
                if (h2 < h0 and l3 < l1 and wave1 >= w1_min_atr * atr[i] and wave3 >= w3_min_ratio * wave1
                        and run_max < l1):
                    r = (high[i] - l3) / wave3
                    if w4_lo <= r <= w4_hi:
                        w4_short[i] = True
            if w2_short[i] or w4_short[i]:
                ext_short[i] = run_max
    return w2_long, w4_long, w2_short, w4_short, ext_long, ext_short


class RsiPinEw(NullSafeSignals, Strategy):
    name = "rsi_pin_ew"

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(deep_merge(DEFAULTS, params))
        if self.params["ew_mode"] not in EW_MODES:
            raise ValueError(f"rsi_pin_ew: ew_mode {self.params['ew_mode']!r}, допустимо {EW_MODES}")
        if self.params["entry_mode"] not in ("setup", "random"):
            raise ValueError(f"rsi_pin_ew: entry_mode {self.params['entry_mode']!r}")

    def warmup_bars(self) -> int:
        p = self.params
        return max(25 * max(int(p["rsi_len"]), int(p["atr_len"])), 60 * (int(p["ew_left"]) + int(p["ew_right"])))

    def compute(self, bars_tf: pl.DataFrame, *, hh_symbol: str, tf: str, htf: pl.DataFrame | None = None,
                bars_1m: pl.DataFrame | None = None, meta: dict[str, Any] | None = None) -> pl.DataFrame:
        p = self.params
        meta = meta or {}
        inst = meta.get("instrument")
        df = prepare_bars(bars_tf, tf, bars_1m)
        if df.is_empty():
            return self._empty()
        halted, blocked_all = bar_flags(df)
        live = ~halted
        sub = df.filter(pl.Series(live))
        blocked = blocked_all[live]
        o = sub["open"].cast(pl.Float64).to_numpy()
        h = sub["high"].cast(pl.Float64).to_numpy()
        lo = sub["low"].cast(pl.Float64).to_numpy()
        c = sub["close"].cast(pl.Float64).to_numpy()
        v = sub["volume"].cast(pl.Float64).fill_null(0.0).to_numpy()
        tb = (sub["taker_buy_volume"].cast(pl.Float64).fill_null(np.nan).to_numpy()
              if "taker_buy_volume" in sub.columns else None)
        ts_ms = ms(sub["ts"])
        close_ms = ms(sub["close_ts"])
        n = len(c)

        ind = indicator_block(h, lo, c, v, tb, ts_ms, p, prop_boundary_min(getattr(inst, "market", "crypto_tradfi")))
        atr = ind["atr"]
        r = ind["rsi14"] if int(p["rsi_len"]) == 14 else core.rsi(c, int(p["rsi_len"]))
        r_prev = core.shift(r, 1)
        hb = htf_block(htf_frame(htf, bars_1m, df, p["htf"]), close_ms, int(p["ema_htf_len"]),
                       int(p["htf_swing_left"]), int(p["htf_swing_right"]))
        _, ph_idx = pivothigh(h, int(p["ew_left"]), int(p["ew_right"]))
        _, pl_idx = pivotlow(lo, int(p["ew_left"]), int(p["ew_right"]))
        w2l, w4l, w2s, w4s, ext_l, ext_s = _elliott_nb(
            h, lo, ph_idx, pl_idx, atr, float(p["w1_min_atr"]), float(p["w2_fib"][0]), float(p["w2_fib"][1]),
            float(p["w4_fib"][0]), float(p["w4_fib"][1]), float(p["w3_min_ratio"]))

        with np.errstate(invalid="ignore", divide="ignore"):
            rng = h - lo
            body = np.abs(c - o)
            pin_ok_bar = (rng > 0) & (rng >= float(p["pin_min_atr"]) * atr) & (body <= float(p["pin_body"]) * rng)
            pin_bull = pin_ok_bar & (np.minimum(o, c) - lo >= float(p["pin_wick"]) * rng)
            pin_bear = pin_ok_bar & (h - np.maximum(o, c) >= float(p["pin_wick"]) * rng)
            plb = max(1, int(p["pin_lookback"]))
            recent = lambda x: core.highest(x.astype(np.float64), plb) > 0  # noqa: E731

            rlb = max(1, int(p["rsi_lookback"]))
            ok = {
                "trend": {"long": hb["htf_trend"] == 1, "short": hb["htf_trend"] == -1},
                "rsi": {"long": (core.lowest(r, rlb) <= float(p["rsi_long_max"])) & (r > r_prev),
                        "short": (core.highest(r, rlb) >= float(p["rsi_short_min"])) & (r < r_prev)},
                "pinbar": {"long": recent(pin_bull), "short": recent(pin_bear)},
            }
            mode = p["ew_mode"]
            use_w2, use_w4 = mode in ("w2", "both", "off"), mode in ("w4", "both", "off")   # off — только для скора
            ok["elliott"] = {"long": (w2l & use_w2) | (w4l & use_w4), "short": (w2s & use_w2) | (w4s & use_w4)}
            use = {"trend": bool(p["use_trend"]), "rsi": bool(p["use_rsi"]), "pinbar": bool(p["use_pin"]),
                   "elliott": mode != "off"}

            ready = ~np.isnan(atr) & (atr > 0) & ~np.isnan(r) & ~np.isnan(r_prev)
            base_ok = ready & ~blocked & session_ok(close_ms, inst, p.get("filters") or {})

            # уровни: опора стопа — минимум (максимум) пинбара и отката после последнего свинга
            ref = {"long": core.lowest(lo, plb), "short": core.highest(h, plb)}
            ref["long"] = np.where(np.isnan(ext_l), ref["long"], np.minimum(ref["long"], ext_l))
            ref["short"] = np.where(np.isnan(ext_s), ref["short"], np.maximum(ref["short"], ext_s))
            levels: dict[str, np.ndarray] = {}
            valid: dict[str, np.ndarray] = {}
            for side, sgn in (("long", 1.0), ("short", -1.0)):
                dist = sgn * (c - ref[side]) + float(p["sl_buf_atr"]) * atr
                dist = np.maximum(dist, float(p["sl_min_atr"]) * atr)
                valid[side] = dist <= float(p["sl_max_atr"]) * atr
                levels[f"entry_{side}"] = np.where(ready, c, np.nan)
                levels[f"sl_{side}"] = c - sgn * dist
                levels[f"tp1_{side}"] = c + sgn * float(p["tp1_r"]) * dist
                levels[f"tp2_{side}"] = (c + sgn * float(p["tp2_r"]) * dist) if p.get("tp2_r") else np.full(n, np.nan)

            score, conds, comps = {}, {}, {}
            for side in ("long", "short"):
                tot = np.zeros(n)
                cond = base_ok & valid[side]
                for f in FACTORS:
                    hit = np.asarray(ok[f][side], dtype=bool)
                    comps[f"c_{f}_{side}"] = np.where(ready, 25.0 * hit, np.nan)
                    tot += 25.0 * hit
                    if use[f]:
                        cond &= hit
                score[side] = np.where(ready, tot, np.nan)
                conds[side] = edge(cond) if p.get("edge_trigger", True) else cond

            if p["entry_mode"] == "random":
                prob = min(1.0, float(p["random_signals_per_day"]) * tf_minutes(tf) / 1440.0)
                u_fire = hash_uniform(int(p["seed"]), hh_symbol, close_ms, 0)
                u_side = hash_uniform(int(p["seed"]), hh_symbol, close_ms, 1)
                fire = base_ok & (u_fire < prob)
                is_long = u_side < 1.0 - float(p["random_short_share"])
                conds = {"long": fire & is_long & valid["long"], "short": fire & ~is_long & valid["short"]}

        cols: dict[str, np.ndarray] = {
            "close": c,
            "score_long": score["long"], "score_short": score["short"],
            "signal_long": conds["long"], "signal_short": conds["short"],
        }
        for side in ("long", "short"):
            for k in ("entry", "sl", "tp1", "tp2"):
                cols[f"{k}_{side}"] = levels[f"{k}_{side}"]
        cols.update({k: comps[k] for k in sorted(comps, key=lambda s: (not s.endswith("_long"), s))})
        cols.update({
            "rsi14": ind["rsi14"], "adx": ind["adx"], "atr_pct": ind["atr_pct"],
            "vwap_dist_pct": ind["vwap_dist_pct"], "htf_trend": hb["htf_trend"], "cvd_trend": ind["cvd_trend"],
            "rsi": r, "rsi_prev": r_prev, "atr": atr,
            "pin_bull": pin_bull, "pin_bear": pin_bear,
            "ew_w2_long": w2l, "ew_w4_long": w4l, "ew_w2_short": w2s, "ew_w4_short": w4s,
            "blocked": blocked,
        })
        return assemble(df, live, cols)

    def _empty(self) -> pl.DataFrame:
        schema: dict[str, Any] = {"ts": TS, "close_ts": TS, "halted": pl.Boolean, "close": pl.Float64,
                                  "score_long": pl.Float64, "score_short": pl.Float64,
                                  "signal_long": pl.Boolean, "signal_short": pl.Boolean}
        for side in ("long", "short"):
            for k in ("entry", "sl", "tp1", "tp2"):
                schema[f"{k}_{side}"] = pl.Float64
        for side in ("long", "short"):
            for k in FACTORS:
                schema[f"c_{k}_{side}"] = pl.Float64
        schema.update({"rsi14": pl.Float64, "adx": pl.Float64, "atr_pct": pl.Float64, "vwap_dist_pct": pl.Float64,
                       "htf_trend": pl.Int8, "cvd_trend": pl.Int8})
        return pl.DataFrame(schema=schema)

    def explain(self, row: dict[str, Any], side: Side) -> list[Factor]:
        up = side == "long"
        labels = {
            "trend": "HTF-тренд " + ("вверх" if up else "вниз"),
            "rsi": f"RSI откатил {'≤' if up else '≥'} {self.params['rsi_long_max' if up else 'rsi_short_min']} "
                   f"и развернулся",
            "pinbar": ("Бычий" if up else "Медвежий") + " пинбар",
            "elliott": "Откат волны " + ("2 → вход в 3" if row.get(f"ew_w2_{side}") else "4 → вход в 5")
                       if (row.get(f"ew_w2_{side}") or row.get(f"ew_w4_{side}")) else "Волна Эллиотта: нет сетапа",
        }
        out = []
        for f in FACTORS:
            pts = row.get(f"c_{f}_{side}")
            pts = 0.0 if pts is None or pts != pts else float(pts)
            out.append(Factor(f, labels[f], None, pts, 25.0, "pro" if pts > 0 else "con"))
        return out
