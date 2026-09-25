"""confluence_v1 — конфлюенс-движок входов, стратегия №1 (раздел 6 ТЗ).

Pine-исходника у нас нет: это СОБСТВЕННАЯ реализация по описанию ТЗ, а не побайтовый порт
Pine-движка. Индикаторы — с Pine-семантикой (quant.indicators), логика скора — наша.

Компоненты скора (каждый даёт долю f ∈ [0, 1] от своего веса; score = 100·Σ w·f / Σ w):
- htf        — HTF-тренд: 0.6 за сторону close HTF относительно EMA(ema_htf_len) HTF
               + 0.4 за структуру HTF (HH/HL для лонга, LH/LL для шорта);
- structure  — свежий слом структуры ТФ (≤ structure_lookback баров) по направлению текущей
               структуры: CHoCH = 1.0, BOS = 0.75;
- sweep      — свежий liquidity sweep (≤ sweep_lookback): прокол swing low с закрытием выше —
               для лонга, swing high — для шорта;
- rsi        — RSI(rsi_len, Wilder). Лонг: RSI растёт и ≤ rsi_mid_long → 1.0; растёт выше
               rsi_mid_long или ниже rsi_os → 0.5; падает или ≥ rsi_ob → 0. Шорт зеркально;
- divergence — свежая (≤ div_lookback) дивергенция RSI по подтверждённым пивотам:
               регулярная 1.0, скрытая 0.8;
- volume     — 0.6·clip((RVOL-1)/(rvol_min-1)) при свече по направлению + 0.4 при CVD-тренде
               по направлению;
- vwap       — 0.5 за сторону close относительно сессионного VWAP + 0.5 за AVWAP от последнего
               подтверждённого свинга (swing low — для лонга): 1.0 — «откат к AVWAP»
               (ближе vwap_near_atr·ATR), 0.5 — по нужную сторону, но далеко.
Вход — score ≥ threshold стороны; шорт — только при медвежьем HTF (short.require_htf_bear).
Стоп — ATR (у шорта шире), TP1/TP2 — R-множители, tp1_frac — доля на TP1.
Фильтры (флаги filters.*): adx_filter (ADX ≥ adx_min), funding_oi_filter (перегрев:
funding ≥ funding_hot или рост OI ≥ oi_hot_pct за oi_lookback_h вместе с ценой),
cvd_divergence_filter (новый максимум цены при отрицательной дельте — не лонговать; зеркально).
edge_trigger: сигнал только на первом баре выполнения условий (без состояния).

Look-ahead: всё считается по закрытым барам; пивоты, BOS/CHoCH, sweep, дивергенции — по
подтверждённым свингам (время события = время подтверждения); HTF — только закрытые бары HTF.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from quant.indicators import core
from quant.indicators import volume as vol
from quant.indicators.pivots import divergences
from quant.indicators.structure import market_structure
from quant.schemas import TS
from quant.strategies.base import Factor, Side, Strategy
from quant.strategies.common import (
    NullSafeSignals, asof_values, assemble, bar_flags, btc_trend_asof, deep_merge, edge, funding_asof, htf_block,
    htf_frame, indicator_block, ms, oi_change_pct, prepare_bars, prop_boundary_min, session_ok,
)

FACTORS = ("htf", "structure", "sweep", "rsi", "divergence", "volume", "vwap")

DEFAULTS: dict[str, Any] = {
    "tf": "1h",
    "htf": "4h",
    # индикаторы
    "ema_htf_len": 200,
    "htf_swing_left": 3,
    "htf_swing_right": 3,
    "swing_left": 5,
    "swing_right": 2,
    "rsi_len": 14,
    "atr_len": 14,
    "adx_len": 14,
    "rvol_len": 20,
    "cvd_len": 24,
    "cvd_thr": 0.05,
    "div_left": 5,
    "div_right": 2,
    "div_range": [5, 60],
    "vwap_anchor": "utc_day",
    # «свежесть» событий, баров ТФ
    "structure_lookback": 6,
    "sweep_lookback": 3,
    "div_lookback": 3,
    # веса компонентов
    "weights": {"htf": 25, "structure": 20, "sweep": 10, "rsi": 10, "divergence": 10, "volume": 10, "vwap": 15},
    "rsi_mid_long": 60,
    "rsi_mid_short": 40,
    "rsi_ob": 70,
    "rsi_os": 30,
    "rvol_min": 1.5,
    "vwap_near_atr": 1.0,
    # пороги фильтров (включение — флагами filters.*)
    "adx_min": 20,
    "funding_hot": 0.0005,
    "oi_lookback_h": 24,
    "oi_hot_pct": 15.0,
    "edge_trigger": True,
    "long": {"threshold": 65, "sl_atr": 1.5, "tp1_r": 1.0, "tp2_r": 2.0, "require_htf_bull": False},
    "short": {"threshold": 65, "sl_atr": 2.0, "tp1_r": 0.8, "tp2_r": 1.5, "require_htf_bear": True},
    "max_hold_min": 1440,
    "valid_min": 60,
    "entry_type": "market",
    "tp1_frac": 0.5,
    "sl_trigger": "last",
    "filters": {"tradfi_off_session_signals": False, "adx_filter": False, "funding_oi_filter": False,
                "cvd_divergence_filter": False},
}


def tf_label(tf: str) -> str:
    """'4h' → '4H', '1d' → '1D', '15m' → '15m' (как на TradingView)."""
    return tf.upper() if tf[-1] in "hd" else tf


def _num(x: Any) -> float | None:
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if np.isnan(f) else f


class ConfluenceV1(NullSafeSignals, Strategy):
    name = "confluence_v1"

    def __init__(self, params: dict[str, Any] | None = None):
        super().__init__(deep_merge(DEFAULTS, params))
        w = self.params["weights"]
        missing = set(FACTORS) - set(w)
        if missing:
            raise ValueError(f"confluence_v1: нет весов {sorted(missing)}")

    # ------------------------------------------------------------ прогрев

    def warmup_bars(self) -> int:
        """Рекомендуемое окно баров ТФ для on_bar: 25×period для RMA-индикаторов (раздел 6)."""
        p = self.params
        per = max(int(p["rsi_len"]), int(p["atr_len"]), 2 * int(p["adx_len"]), int(p["rvol_len"]), int(p["cvd_len"]))
        return max(25 * per, 20 * (int(p["swing_left"]) + int(p["swing_right"])), 5 * int(p["div_range"][1]))

    def htf_warmup_bars(self) -> int:
        """Баров HTF для сходимости EMA HTF (25×period); на практике — вся история HTF."""
        return 25 * int(self.params["ema_htf_len"])

    # ------------------------------------------------------------ расчёт

    def compute(self, bars_tf: pl.DataFrame, *, hh_symbol: str, tf: str, htf: pl.DataFrame | None = None,
                bars_1m: pl.DataFrame | None = None, meta: dict[str, Any] | None = None) -> pl.DataFrame:
        p = self.params
        meta = meta or {}
        inst = meta.get("instrument")
        filters = p.get("filters") or {}
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

        boundary = prop_boundary_min(getattr(inst, "market", "crypto_tradfi"))
        ind = indicator_block(h, lo, c, v, tb, ts_ms, p, boundary)
        r = ind["rsi14"] if int(p["rsi_len"]) == 14 else core.rsi(c, int(p["rsi_len"]))
        r_prev = core.shift(r, 1)
        atr = ind["atr"]

        hdf = htf_frame(htf, bars_1m, df, p["htf"])
        hb = htf_block(hdf, close_ms, int(p["ema_htf_len"]), int(p["htf_swing_left"]), int(p["htf_swing_right"]))

        st = market_structure(h, lo, c, int(p["swing_left"]), int(p["swing_right"]))
        src = core.hlc3(h, lo, c)
        av_long = vol.avwap_dynamic(src, v, st.swing_low_idx)
        av_short = vol.avwap_dynamic(src, v, st.swing_high_idx)
        dv = divergences(r, lo, h, int(p["div_left"]), int(p["div_right"]), int(p["div_range"][0]),
                         int(p["div_range"][1]))
        bs = {k: core.barssince(a) for k, a in {
            "bs_bos_up": st.bos_up, "bs_bos_dn": st.bos_dn, "bs_choch_up": st.choch_up,
            "bs_choch_dn": st.choch_dn, "bs_sweep_up": st.sweep_up, "bs_sweep_dn": st.sweep_dn,
            "bs_div_bull": dv.bull, "bs_hdiv_bull": dv.hidden_bull, "bs_div_bear": dv.bear,
            "bs_hdiv_bear": dv.hidden_bear}.items()}

        with np.errstate(invalid="ignore", divide="ignore"):
            f = self._factors(o, c, r, r_prev, atr, ind, hb, st, bs, av_long, av_short)
            w = {k: float(p["weights"][k]) for k in FACTORS}
            wsum = sum(w.values()) or 1.0
            ready = ~np.isnan(atr) & ~np.isnan(r) & ~np.isnan(r_prev) & ~np.isnan(ind["rvol"])
            cols_c: dict[str, np.ndarray] = {}
            score = {}
            for side in ("long", "short"):
                tot = np.zeros(n)
                for k in FACTORS:
                    pts = w[k] * f[k][side]
                    tot += pts
                    cols_c[f"c_{k}_{side}"] = np.where(ready, pts, np.nan)
                score[side] = np.where(ready, 100.0 * tot / wsum, np.nan)

            # внешние ряды и фильтры
            lb_ms = int(float(p["oi_lookback_h"]) * 3_600_000)
            rate = funding_asof(meta.get("funding"), close_ms)
            oi_chg = oi_change_pct(meta.get("metrics"), close_ms, lb_ms)
            px_then = asof_values(close_ms, c, close_ms - lb_ms)
            px_chg = (c / px_then - 1.0) * 100.0
            hot = float(p["funding_hot"])
            oi_surge = oi_chg >= float(p["oi_hot_pct"])
            overheat_long = (rate >= hot) | (oi_surge & (px_chg > 0))
            overheat_short = (rate <= -hot) | (oi_surge & (px_chg < 0))
            btc = btc_trend_asof(meta.get("btc_trend"), close_ms)
            sess = session_ok(close_ms, inst, filters)

            base_ok = ready & hb["htf_ready"] & ~blocked & sess & (atr > 0)
            conds = {}
            for side, sgn in (("long", 1), ("short", -1)):
                sp = p[side]
                cond = base_ok & (score[side] >= float(sp["threshold"]))
                if side == "long" and sp.get("require_htf_bull", False):
                    cond &= hb["htf_trend"] == 1
                if side == "short" and sp.get("require_htf_bear", True):
                    cond &= hb["htf_trend"] == -1
                if filters.get("adx_filter", False):
                    cond &= ind["adx"] >= float(p["adx_min"])
                if filters.get("funding_oi_filter", False):
                    cond &= ~(overheat_long if side == "long" else overheat_short)
                if filters.get("cvd_divergence_filter", False):
                    cond &= ~(ind["cvd_div_bear"] if side == "long" else ind["cvd_div_bull"])
                conds[side] = edge(cond) if p.get("edge_trigger", True) else cond

            levels = {}
            for side, sgn in (("long", 1.0), ("short", -1.0)):
                sp = p[side]
                sl = c - sgn * float(sp["sl_atr"]) * atr
                risk = np.abs(c - sl)
                tp2_r = sp.get("tp2_r")
                levels[f"entry_{side}"] = np.where(np.isnan(atr), np.nan, c)
                levels[f"sl_{side}"] = sl
                levels[f"tp1_{side}"] = c + sgn * float(sp["tp1_r"]) * risk
                levels[f"tp2_{side}"] = (c + sgn * float(tp2_r) * risk) if tp2_r else np.full(n, np.nan)

        cols: dict[str, np.ndarray] = {
            "close": c,
            "score_long": score["long"], "score_short": score["short"],
            "signal_long": conds["long"], "signal_short": conds["short"],
        }
        for side in ("long", "short"):
            for k in ("entry", "sl", "tp1", "tp2"):
                cols[f"{k}_{side}"] = levels[f"{k}_{side}"]
        cols.update({k: cols_c[k] for k in sorted(cols_c, key=lambda s: (not s.endswith("_long"), s))})
        cols.update({
            # колонки скринера
            "rsi14": ind["rsi14"], "adx": ind["adx"], "atr_pct": ind["atr_pct"],
            "vwap_dist_pct": ind["vwap_dist_pct"], "htf_trend": hb["htf_trend"], "cvd_trend": ind["cvd_trend"],
            # детали для explain / графика
            "rsi": r, "rsi_prev": r_prev, "atr": atr, "plus_di": ind["plus_di"], "minus_di": ind["minus_di"],
            "vwap": ind["vwap"], "avwap_long": av_long, "avwap_short": av_short, "rvol": ind["rvol"],
            "cvd": ind["cvd"], "cvd_ratio": ind["cvd_ratio"],
            "cvd_div_bull": ind["cvd_div_bull"], "cvd_div_bear": ind["cvd_div_bear"],
            "htf_close": hb["htf_close"], "htf_ema": hb["htf_ema"], "htf_ema_side": hb["htf_ema_side"],
            "htf_struct": hb["htf_struct"], "struct_trend": st.trend,
            "swing_high": st.swing_high, "swing_low": st.swing_low,
            **bs,
            "funding_rate": rate, "oi_change_pct": oi_chg, "px_change_pct": px_chg,
            "overheat_long": overheat_long, "overheat_short": overheat_short,
            "btc_trend": btc, "session_open": sess, "blocked": blocked,
        })
        return assemble(df, live, cols)

    def _factors(self, o, c, r, r_prev, atr, ind, hb, st, bs, av_long, av_short) -> dict[str, dict[str, np.ndarray]]:
        p = self.params

        def recent(name: str, lb: int) -> np.ndarray:
            return bs[name] < lb          # NaN (события не было) → False

        es, hs = hb["htf_ema_side"], hb["htf_struct"]
        f: dict[str, dict[str, np.ndarray]] = {}
        f["htf"] = {"long": 0.6 * (es > 0) + 0.4 * (hs > 0), "short": 0.6 * (es < 0) + 0.4 * (hs < 0)}

        lb = int(p["structure_lookback"])
        f["structure"] = {
            "long": np.where(st.trend == 1, np.where(recent("bs_choch_up", lb), 1.0,
                                                     np.where(recent("bs_bos_up", lb), 0.75, 0.0)), 0.0),
            "short": np.where(st.trend == -1, np.where(recent("bs_choch_dn", lb), 1.0,
                                                       np.where(recent("bs_bos_dn", lb), 0.75, 0.0)), 0.0),
        }
        slb = int(p["sweep_lookback"])
        f["sweep"] = {"long": recent("bs_sweep_up", slb).astype(float),
                      "short": recent("bs_sweep_dn", slb).astype(float)}

        ob, os_ = float(p["rsi_ob"]), float(p["rsi_os"])
        rising, falling = r > r_prev, r < r_prev
        f["rsi"] = {
            "long": np.where(r >= ob, 0.0, np.where(rising, np.where(r < os_, 0.5,
                             np.where(r <= float(p["rsi_mid_long"]), 1.0, 0.5)), 0.0)),
            "short": np.where(r <= os_, 0.0, np.where(falling, np.where(r > ob, 0.5,
                              np.where(r >= float(p["rsi_mid_short"]), 1.0, 0.5)), 0.0)),
        }
        dlb = int(p["div_lookback"])
        f["divergence"] = {
            "long": np.where(recent("bs_div_bull", dlb), 1.0, np.where(recent("bs_hdiv_bull", dlb), 0.8, 0.0)),
            "short": np.where(recent("bs_div_bear", dlb), 1.0, np.where(recent("bs_hdiv_bear", dlb), 0.8, 0.0)),
        }
        rv_min = max(float(p["rvol_min"]), 1.0 + 1e-9)
        rv_part = np.nan_to_num(np.clip((ind["rvol"] - 1.0) / (rv_min - 1.0), 0.0, 1.0))
        ct = ind["cvd_trend"]
        f["volume"] = {"long": 0.6 * rv_part * (c > o) + 0.4 * (ct == 1),
                       "short": 0.6 * rv_part * (c < o) + 0.4 * (ct == -1)}
        near = float(p["vwap_near_atr"]) * atr
        vw = ind["vwap"]
        g_long = np.where(c > av_long, np.where(c - av_long <= near, 1.0, 0.5), 0.0)
        g_short = np.where(c < av_short, np.where(av_short - c <= near, 1.0, 0.5), 0.0)
        f["vwap"] = {"long": 0.5 * (c > vw) + 0.5 * g_long, "short": 0.5 * (c < vw) + 0.5 * g_short}
        return f

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

    # ------------------------------------------------------------ разбор

    def explain(self, row: dict[str, Any], side: Side) -> list[Factor]:
        p = self.params
        w = p["weights"]
        want = 1 if side == "long" else -1
        L = side == "long"
        tfl = tf_label(str(p.get("tf") or "1h"))
        htfl = tf_label(str(p.get("htf") or "4h"))
        out: list[Factor] = []

        def pts(k: str) -> float:
            return _num(row.get(f"c_{k}_{side}")) or 0.0

        def recent(name: str, lb: int) -> bool:
            x = _num(row.get(name))
            return x is not None and x < lb

        # HTF
        es = _num(row.get("htf_ema_side"))
        hs = _num(row.get("htf_struct"))
        if _num(row.get("htf_ema")) is None:
            lbl, verdict = f"HTF: нет данных (EMA{p['ema_htf_len']} {htfl})", "neutral"
        else:
            word = "аптренд" if es and es > 0 else ("даунтренд" if es and es < 0 else "у EMA")
            lbl = f"HTF {word} (EMA{p['ema_htf_len']} {htfl})"
            if hs == 1:
                lbl += ", структура HH/HL"
            elif hs == -1:
                lbl += ", структура LH/LL"
            verdict = "pro" if es == want else ("con" if es == -want else "neutral")
        out.append(Factor("htf", lbl, _num(row.get("htf_trend")), pts("htf"), float(w["htf"]), verdict))

        # BOS / CHoCH
        lb = int(p["structure_lookback"])
        up, dn = ("up", "dn") if L else ("dn", "up")
        st_tr = _num(row.get("struct_trend"))
        if pts("structure") > 0:
            kind = "CHoCH" if recent(f"bs_choch_{up}", lb) else "BOS"
            lbl, verdict, val = f"{'Бычий' if L else 'Медвежий'} {kind} ({tfl})", "pro", kind
        elif st_tr == -want:
            lbl = f"Структура {tfl} {'медвежья' if L else 'бычья'} (последний слом {'вниз' if L else 'вверх'})"
            verdict, val = "con", None
        else:
            lbl, verdict, val = f"Нет свежего слома структуры ({tfl})", "neutral", None
        out.append(Factor("structure", lbl, val, pts("structure"), float(w["structure"]), verdict))

        # sweep
        slb = int(p["sweep_lookback"])
        if pts("sweep") > 0:
            lbl = "Снятие ликвидности под swing low" if L else "Снятие ликвидности над swing high"
            verdict = "pro"
        elif recent(f"bs_sweep_{dn}", slb):
            lbl = "Sweep над swing high — против лонга" if L else "Sweep под swing low — против шорта"
            verdict = "con"
        else:
            lbl, verdict = "Sweep ликвидности нет", "neutral"
        out.append(Factor("sweep", lbl, None, pts("sweep"), float(w["sweep"]), verdict))

        # RSI
        r, rp = _num(row.get("rsi")), _num(row.get("rsi_prev"))
        if r is None:
            out.append(Factor("rsi", "RSI: нет данных", None, 0.0, float(w["rsi"]), "neutral"))
        else:
            arrow = "↑" if rp is not None and r > rp else ("↓" if rp is not None and r < rp else "→")
            ob, os_ = float(p["rsi_ob"]), float(p["rsi_os"])
            with_dir = (arrow == "↑") if L else (arrow == "↓")
            if (L and r >= ob) or (not L and r <= os_):
                lbl, verdict = f"RSI {r:.0f} — {'перекупленность' if L else 'перепроданность'}", "con"
            elif not with_dir:
                lbl, verdict = f"RSI {r:.0f}{arrow} — импульс {'вниз' if L else 'вверх'}", "con"
            elif (L and r < os_) or (not L and r > ob):
                lbl = f"RSI {r:.0f}{arrow} — выход из {'перепроданности' if L else 'перекупленности'}"
                verdict = "pro"
            elif pts("rsi") >= float(w["rsi"]) - 1e-9:
                lbl, verdict = f"RSI {r:.0f}{arrow}", "pro"
            else:
                lbl = f"RSI {r:.0f}{arrow} — близко к {'перекупленности' if L else 'перепроданности'}"
                verdict = "neutral"
            out.append(Factor("rsi", lbl, round(r, 1), pts("rsi"), float(w["rsi"]), verdict))

        # дивергенции
        dlb = int(p["div_lookback"])
        bull, bear = ("bull", "bear") if L else ("bear", "bull")
        adj = {"bull": "бычья", "bear": "медвежья"}
        if recent(f"bs_div_{bull}", dlb):
            lbl, verdict = f"{adj[bull].capitalize()} дивергенция RSI", "pro"
        elif recent(f"bs_hdiv_{bull}", dlb):
            lbl, verdict = f"Скрытая {adj[bull]} дивергенция RSI", "pro"
        elif recent(f"bs_div_{bear}", dlb):
            lbl, verdict = f"{adj[bear].capitalize()} дивергенция RSI", "con"
        elif recent(f"bs_hdiv_{bear}", dlb):
            lbl, verdict = f"Скрытая {adj[bear]} дивергенция RSI", "con"
        else:
            lbl, verdict = "Дивергенций RSI нет", "neutral"
        out.append(Factor("divergence", lbl, None, pts("divergence"), float(w["divergence"]), verdict))

        # объём / CVD
        rv = _num(row.get("rvol"))
        ct = _num(row.get("cvd_trend"))
        parts = []
        if rv is not None:
            parts.append(f"RVOL {rv:.1f}")
        if ct is not None:
            parts.append({1: "CVD растёт", -1: "CVD падает"}.get(int(ct), "CVD нейтрален"))
        lbl = ", ".join(parts) or "Объём: нет данных"
        if pts("volume") >= 0.5 * float(w["volume"]):
            verdict = "pro"
        elif ct == -want:
            verdict = "con"
        else:
            verdict = "neutral"
        out.append(Factor("volume", lbl, rv, pts("volume"), float(w["volume"]), verdict))

        # VWAP / AVWAP
        c, vw = _num(row.get("close")), _num(row.get("vwap"))
        av = _num(row.get(f"avwap_{side}"))
        a = _num(row.get("atr"))
        parts = []
        if c is not None and vw is not None:
            parts.append("выше VWAP" if c > vw else "ниже VWAP")
        if c is not None and av is not None:
            good = c > av if L else c < av
            near = a is not None and abs(c - av) <= float(p["vwap_near_atr"]) * a
            if good:
                parts.append("откат к AVWAP" if near else ("выше AVWAP" if L else "ниже AVWAP"))
            else:
                parts.append("ниже AVWAP" if L else "выше AVWAP")
        lbl = ("Цена " + ", ".join(parts)) if parts else "VWAP: нет данных"
        pv = pts("vwap")
        verdict = "pro" if pv >= 0.5 * float(w["vwap"]) else ("con" if parts and pv == 0 else "neutral")
        out.append(Factor("vwap", lbl, _num(row.get("vwap_dist_pct")), pv, float(w["vwap"]), verdict))

        out.extend(self._info_factors(row, side))
        return out

    def _info_factors(self, row: dict[str, Any], side: Side) -> list[Factor]:
        """Факторы без баллов (фильтры и контекст): max_points = 0."""
        p = self.params
        L = side == "long"
        out: list[Factor] = []
        adx = _num(row.get("adx"))
        if adx is not None:
            weak = adx < float(p["adx_min"])
            out.append(Factor("adx", f"ADX {adx:.0f} — {'тренд слабый' if weak else 'тренд есть'}", round(adx, 1),
                              0.0, 0.0, "con" if weak else "pro"))
        rate = _num(row.get("funding_rate"))
        if rate is not None:
            hot = float(p["funding_hot"])
            if (L and rate >= hot) or (not L and rate <= -hot):
                lbl, verdict = f"Funding {rate * 100:+.4f}% — перегрев {'лонгов' if L else 'шортов'}", "con"
            elif abs(rate) >= hot:
                lbl, verdict = f"Funding {rate * 100:+.4f}% — перегрев {'шортов' if L else 'лонгов'}", "pro"
            else:
                lbl, verdict = f"Funding нейтральный ({rate * 100:+.4f}%)", "pro"
            out.append(Factor("funding", lbl, rate, 0.0, 0.0, verdict))
        oi = _num(row.get("oi_change_pct"))
        if oi is not None:
            px = _num(row.get("px_change_pct")) or 0.0
            surge = oi >= float(p["oi_hot_pct"])
            crowded = surge and ((px > 0) if L else (px < 0))
            lbl = f"OI {oi:+.1f}% за {int(float(p['oi_lookback_h']))}ч"
            out.append(Factor("oi", lbl + (" — перегрев" if crowded else ""), round(oi, 2), 0.0, 0.0,
                              "con" if crowded else "neutral"))
        div = row.get("cvd_div_bear" if L else "cvd_div_bull")
        if div:
            out.append(Factor("cvd_divergence",
                              "Медвежья дивергенция CVD (новый максимум при отрицательной дельте)" if L
                              else "Бычья дивергенция CVD (новый минимум при положительной дельте)",
                              None, 0.0, 0.0, "con"))
        btc = _num(row.get("btc_trend"))
        if btc is not None:
            want = 1 if L else -1
            lbl = {1: "BTC в аптренде", -1: "BTC в даунтренде"}.get(int(btc), "BTC во флэте")
            out.append(Factor("btc_trend", lbl, int(btc), 0.0, 0.0,
                              "pro" if btc == want else ("con" if btc == -want else "neutral")))
        if row.get("session_open") is False:
            out.append(Factor("session", "Сессия базового рынка закрыта", None, 0.0, 0.0, "con"))
        if row.get("halted"):
            out.append(Factor("halted", "Торговля остановлена (halted)", None, 0.0, 0.0, "con"))
        if row.get("blocked"):
            out.append(Factor("synthetic", "Синтетический бар — сигналы выключены", None, 0.0, 0.0, "con"))
        return out
