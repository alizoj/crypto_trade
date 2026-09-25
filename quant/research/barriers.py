"""Исполнение сигналов на 1m-барах: метка тройного барьера, MFE/MAE, путь mark-цен, funding.

Единое ядро для:
- уровня 1 исследования (векторный скрининг: метки TP/SL/таймаут + MFE/MAE), раздел 7;
- ступени A бэктеста (сделки, не зависящие от состояния счёта), раздел 5.

Модель исполнения (раздел 5 ТЗ, консервативно):
- вход не раньше open 1m-бара в момент ts сигнала + задержка ручного исполнения;
- market — open бара ± slippage (taker); limit — fill только при прохождении цены сквозь уровень
  (maker), если уровень уже пройден в момент выставления — исполняется как market;
- SL — stop-market: цена стопа (или open при гэпе) ∓ slippage; TP — trigger-market по цене тейка ∓ slippage;
- триггер SL/TP — по last (h/l) или mark (m_h/m_l);
- SL и TP достижимы в одном баре → SL (intrabar_resolution=sl_first), флаг ambiguous;
- бары halted/synthetic не дают исполнения;
- если к моменту входа цена уже за SL или TP — сделка не открывается (invalid).
Метка (label): 1 — TP1 раньше SL, 0 — SL раньше TP1, 2 — таймаут без TP1/SL.
"""

from __future__ import annotations

import zlib
from dataclasses import dataclass

import numpy as np
import polars as pl
from numba import njit

# коды статуса сигнала
FILLED, NO_FILL, INVALID, NO_DATA = 0, 1, 2, 3
# коды выхода
EXIT_TP1, EXIT_TP2, EXIT_SL, EXIT_TIMEOUT, EXIT_BE = 1, 2, 3, 4, 5
EXIT_NAMES = {EXIT_TP1: "tp1", EXIT_TP2: "tp2", EXIT_SL: "sl", EXIT_TIMEOUT: "timeout", EXIT_BE: "be_stop"}
LABEL_NAMES = {1: "tp", 0: "sl", 2: "timeout", -1: None}


@njit(cache=True)
def _first_good(ts, bad, i, t_limit):
    n = len(ts)
    while i < n and ts[i] <= t_limit:
        if not bad[i]:
            return i
        i += 1
    return -1


@njit(cache=True)
def _pass1(ts, o, h, l, c, mh, ml, bad, slip,
           s_ts, s_side, s_limit, s_entry, s_sl, s_tp1, s_tp2, s_frac, s_hold, s_valid, s_delay,
           trig_mark, be_after_tp1, max_wait):
    n = len(s_ts)
    nb = len(ts)
    status = np.full(n, NO_DATA, np.int8)
    ie = np.full(n, -1, np.int64)
    ix = np.full(n, -1, np.int64)
    e_min = np.zeros(n, np.int64)
    e_px = np.full(n, np.nan)
    e_slip = np.zeros(n)
    e_liq = np.zeros(n, np.int8)          # 0 maker, 1 taker
    x1_min = np.full(n, -1, np.int64)
    x1_px = np.full(n, np.nan)
    x1_slip = np.zeros(n)
    x1_frac = np.zeros(n)
    x1_kind = np.zeros(n, np.int8)
    x2_min = np.full(n, -1, np.int64)
    x2_px = np.full(n, np.nan)
    x2_slip = np.zeros(n)
    x2_frac = np.zeros(n)
    x2_kind = np.zeros(n, np.int8)
    label = np.full(n, -1, np.int8)
    ambiguous = np.zeros(n, np.bool_)
    mfe = np.full(n, np.nan)
    mae = np.full(n, np.nan)
    for k in range(n):
        side = s_side[k]
        sl0 = s_sl[k]
        tp1 = s_tp1[k]
        tp2 = s_tp2[k]
        t0 = s_ts[k] + s_delay[k]
        i = np.searchsorted(ts, t0)
        if i >= nb:
            continue
        fill = np.nan
        fslip = 0.0
        liq = 1
        ient = -1
        if s_limit[k]:
            lim = s_entry[k]
            t_end = t0 + s_valid[k]
            first = True
            j = i
            while j < nb and ts[j] < t_end:
                if bad[j]:
                    j += 1
                    continue
                if first:
                    first = False
                    marketable = (o[j] <= lim) if side > 0 else (o[j] >= lim)
                    if marketable:
                        fslip = o[j] * slip[j]
                        fill = o[j] + side * fslip
                        liq = 1
                        ient = j
                        break
                touched = (l[j] < lim) if side > 0 else (h[j] > lim)
                if touched:
                    fill = lim
                    fslip = 0.0
                    liq = 0
                    ient = j
                    break
                j += 1
            if ient < 0:
                status[k] = NO_FILL
                continue
        else:
            j = _first_good(ts, bad, i, t0 + max_wait)
            if j < 0:
                status[k] = NO_DATA
                continue
            fslip = o[j] * slip[j]
            fill = o[j] + side * fslip
            liq = 1
            ient = j
        # валидность: цена уже за SL или за TP1
        if side > 0:
            if fill <= sl0 or fill >= tp1:
                status[k] = INVALID
                continue
        else:
            if fill >= sl0 or fill <= tp1:
                status[k] = INVALID
                continue
        risk = abs(fill - sl0)
        if risk <= 0:
            status[k] = INVALID
            continue
        status[k] = FILLED
        ie[k] = ient
        e_min[k] = ts[ient]
        e_px[k] = fill
        e_slip[k] = fslip
        e_liq[k] = liq
        has_tp2 = not np.isnan(tp2)
        frac1 = s_frac[k] if has_tp2 else 1.0
        cur_sl = sl0
        tp1_done = False
        remaining = 1.0
        hi_max = -1e300
        lo_min = 1e300
        t_timeout = e_min[k] + s_hold[k]
        j = ient
        done = False
        while j < nb:
            if bad[j]:
                j += 1
                continue
            if ts[j] >= t_timeout:
                # таймаут: выход по open
                sl_amt = o[j] * slip[j]
                px = o[j] - side * sl_amt
                if not tp1_done:
                    x1_min[k] = ts[j]; x1_px[k] = px; x1_slip[k] = sl_amt; x1_frac[k] = remaining; x1_kind[k] = EXIT_TIMEOUT
                else:
                    x2_min[k] = ts[j]; x2_px[k] = px; x2_slip[k] = sl_amt; x2_frac[k] = remaining; x2_kind[k] = EXIT_TIMEOUT
                if label[k] < 0:
                    label[k] = 2
                ix[k] = j
                done = True
                break
            if h[j] > hi_max:
                hi_max = h[j]
            if l[j] < lo_min:
                lo_min = l[j]
            if trig_mark and not np.isnan(mh[j]):
                thi = mh[j]
                tlo = ml[j]
            else:
                thi = h[j]
                tlo = l[j]
            cur_tp = tp1 if not tp1_done else tp2
            if side > 0:
                sl_hit = tlo <= cur_sl
                tp_hit = thi >= cur_tp
            else:
                sl_hit = thi >= cur_sl
                tp_hit = tlo <= cur_tp
            if sl_hit and tp_hit:
                ambiguous[k] = True
            if sl_hit:
                gap = (o[j] < cur_sl) if side > 0 else (o[j] > cur_sl)
                raw = o[j] if gap else cur_sl
                sl_amt = raw * slip[j]
                px = raw - side * sl_amt
                kind = EXIT_BE if (tp1_done and cur_sl != sl0) else EXIT_SL
                if not tp1_done:
                    x1_min[k] = ts[j]; x1_px[k] = px; x1_slip[k] = sl_amt; x1_frac[k] = remaining; x1_kind[k] = kind
                else:
                    x2_min[k] = ts[j]; x2_px[k] = px; x2_slip[k] = sl_amt; x2_frac[k] = remaining; x2_kind[k] = kind
                if label[k] < 0:
                    label[k] = 0
                ix[k] = j
                done = True
                break
            if tp_hit:
                sl_amt = cur_tp * slip[j]
                px = cur_tp - side * sl_amt
                if not tp1_done:
                    if label[k] < 0:
                        label[k] = 1
                    if has_tp2 and frac1 < 1.0:
                        x1_min[k] = ts[j]; x1_px[k] = px; x1_slip[k] = sl_amt; x1_frac[k] = frac1; x1_kind[k] = EXIT_TP1
                        remaining = 1.0 - frac1
                        tp1_done = True
                        if be_after_tp1:
                            cur_sl = fill
                        # в том же баре может быть достигнут и TP2
                        thi2 = thi
                        tlo2 = tlo
                        tp2_hit = (thi2 >= tp2) if side > 0 else (tlo2 <= tp2)
                        if tp2_hit:
                            sl_amt2 = tp2 * slip[j]
                            px2 = tp2 - side * sl_amt2
                            x2_min[k] = ts[j]; x2_px[k] = px2; x2_slip[k] = sl_amt2; x2_frac[k] = remaining; x2_kind[k] = EXIT_TP2
                            ix[k] = j
                            done = True
                            break
                    else:
                        x1_min[k] = ts[j]; x1_px[k] = px; x1_slip[k] = sl_amt; x1_frac[k] = 1.0; x1_kind[k] = EXIT_TP1
                        ix[k] = j
                        done = True
                        break
                else:
                    x2_min[k] = ts[j]; x2_px[k] = px; x2_slip[k] = sl_amt; x2_frac[k] = remaining; x2_kind[k] = EXIT_TP2
                    ix[k] = j
                    done = True
                    break
            j += 1
        if not done:
            # данные кончились: закрытие по последнему close
            jl = nb - 1
            while jl > ient and bad[jl]:
                jl -= 1
            sl_amt = c[jl] * slip[jl]
            px = c[jl] - side * sl_amt
            if not tp1_done:
                x1_min[k] = ts[jl]; x1_px[k] = px; x1_slip[k] = sl_amt; x1_frac[k] = remaining; x1_kind[k] = EXIT_TIMEOUT
            else:
                x2_min[k] = ts[jl]; x2_px[k] = px; x2_slip[k] = sl_amt; x2_frac[k] = remaining; x2_kind[k] = EXIT_TIMEOUT
            if label[k] < 0:
                label[k] = 2
            ix[k] = jl
        if hi_max > -1e299:
            if side > 0:
                mfe[k] = (hi_max - fill) / risk
                mae[k] = (fill - lo_min) / risk
            else:
                mfe[k] = (fill - lo_min) / risk
                mae[k] = (hi_max - fill) / risk
    return (status, ie, ix, e_min, e_px, e_slip, e_liq,
            x1_min, x1_px, x1_slip, x1_frac, x1_kind,
            x2_min, x2_px, x2_slip, x2_frac, x2_kind,
            label, ambiguous, mfe, mae)


@njit(cache=True)
def _fill_paths(ts, o, h, l, c, mo, mh, ml, mc, bad, status, side, ie, ix, x_final_px, x_final_kind):
    n = len(status)
    lens = np.zeros(n, np.int64)
    for k in range(n):
        if status[k] != FILLED:
            continue
        cnt = 0
        for j in range(ie[k], ix[k] + 1):
            if not bad[j]:
                cnt += 1
        lens[k] = cnt
    total = lens.sum()
    starts = np.zeros(n, np.int64)
    acc = 0
    for k in range(n):
        starts[k] = acc
        acc += lens[k]
    p_ts = np.zeros(total, np.int64)
    p_wm_lo = np.zeros(total)
    p_wm_hi = np.zeros(total)
    p_mc = np.zeros(total)
    p_wl_lo = np.zeros(total)
    p_wl_hi = np.zeros(total)
    p_lc = np.zeros(total)
    for k in range(n):
        if status[k] != FILLED:
            continue
        p = starts[k]
        for j in range(ie[k], ix[k] + 1):
            if bad[j]:
                continue
            m_lo = ml[j] if not np.isnan(ml[j]) else l[j]
            m_hi = mh[j] if not np.isnan(mh[j]) else h[j]
            m_c = mc[j] if not np.isnan(mc[j]) else c[j]
            p_ts[p] = ts[j]
            p_wm_lo[p] = m_lo
            p_wm_hi[p] = m_hi
            p_mc[p] = m_c
            p_wl_lo[p] = l[j]
            p_wl_hi[p] = h[j]
            p_lc[p] = c[j]
            if j == ix[k]:
                kind = x_final_kind[k]
                if kind == EXIT_SL or kind == EXIT_BE or kind == EXIT_TIMEOUT:
                    px = x_final_px[k]
                    # позиция закрыта по цене выхода: хуже неё в этом баре позиция не была
                    if side[k] > 0:
                        p_wm_lo[p] = px
                        p_wl_lo[p] = px
                    else:
                        p_wm_hi[p] = px
                        p_wl_hi[p] = px
                    p_mc[p] = px
                    p_lc[p] = px
            p += 1
    return starts, lens, p_ts, p_wm_lo, p_wm_hi, p_mc, p_wl_lo, p_wl_hi, p_lc


@njit(cache=True)
def _fill_funding(status, side, e_min, x_final_min, f_ts, f_rate, f_mark):
    n = len(status)
    lens = np.zeros(n, np.int64)
    for k in range(n):
        if status[k] != FILLED:
            continue
        a = np.searchsorted(f_ts, e_min[k], side="right")     # строго после входа
        b = np.searchsorted(f_ts, x_final_min[k], side="right")  # включая минуту выхода
        lens[k] = max(0, b - a)
    total = lens.sum()
    starts = np.zeros(n, np.int64)
    acc = 0
    for k in range(n):
        starts[k] = acc
        acc += lens[k]
    fm = np.zeros(total, np.int64)
    famt = np.zeros(total)
    for k in range(n):
        if lens[k] == 0:
            continue
        a = np.searchsorted(f_ts, e_min[k], side="right")
        p = starts[k]
        for q in range(lens[k]):
            fm[p] = f_ts[a + q]
            famt[p] = -side[k] * f_mark[a + q] * f_rate[a + q]   # на 1 единицу; лонг платит при rate > 0
            p += 1
    return starts, lens, fm, famt


@njit(cache=True)
def _rma(x, n):
    out = np.full(len(x), np.nan)
    if len(x) < n:
        return out
    s = 0.0
    for i in range(n):
        s += x[i]
    out[n - 1] = s / n
    a = 1.0 / n
    for i in range(n, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def atr_1m(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int = 14) -> np.ndarray:
    prev = np.concatenate([[np.nan], c[:-1]])
    tr = np.where(np.isnan(prev), h - l, np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev))))
    out = _rma(tr.astype(np.float64), n)
    return np.where(np.isnan(out), tr, out)


@dataclass
class ExecParams:
    exec_delay_min: tuple[int, int] = (1, 10)
    sl_tp_trigger: str = "last"              # last | mark
    intrabar_resolution: str = "sl_first"    # sl_first | ticks
    slippage_k_atr: float = 0.05
    slippage_ref_notional_usd: float = 10_000.0
    slippage_mult: float = 1.0
    delay_mult: float = 1.0
    be_after_tp1: bool = False
    max_entry_wait_min: int = 5
    seed: int = 7

    @classmethod
    def from_config(cls, execution: dict | None, **overrides) -> "ExecParams":
        e = dict(execution or {})
        d = e.get("exec_delay_min", (1, 10))
        p = cls(exec_delay_min=(int(d[0]), int(d[1])),
                sl_tp_trigger=e.get("sl_tp_trigger", "last"),
                intrabar_resolution=e.get("intrabar_resolution", "sl_first"),
                slippage_k_atr=float(e.get("slippage_k_atr", 0.05)),
                slippage_ref_notional_usd=float(e.get("slippage_ref_notional_usd", 10_000)),
                be_after_tp1=bool(e.get("be_after_tp1", False)))
        for k, v in overrides.items():
            setattr(p, k, v)
        return p


def slippage_fraction(bars: pl.DataFrame, params: ExecParams, book_depth: pl.DataFrame | None = None) -> np.ndarray:
    """Доля проскальзывания на каждую минуту: max(k·ATR_1m/price, влияние на стакан для эталонного notional).

    Влияние по bookDepth: уровень (в %), на котором кумулятивный notional покрывает эталонный размер,
    линейная интерполяция по полосам ±0.2…±5%; средняя цена исполнения ≈ половина уровня.
    """
    h = bars["high"].to_numpy().astype(np.float64)
    l = bars["low"].to_numpy().astype(np.float64)
    c = bars["close"].to_numpy().astype(np.float64)
    atr = atr_1m(h, l, c)
    base = params.slippage_k_atr * np.divide(atr, c, out=np.zeros_like(c), where=c > 0)
    base = np.nan_to_num(base, nan=0.0)
    if book_depth is not None and not book_depth.is_empty():
        depth = _depth_impact(bars, book_depth, params.slippage_ref_notional_usd)
        base = np.maximum(base, depth)
    return base * params.slippage_mult


def _depth_impact(bars: pl.DataFrame, bd: pl.DataFrame, ref: float) -> np.ndarray:
    # по снимку: средняя по сторонам кумулятивная глубина в полосах
    snap = (bd.with_columns(pl.col("percentage").abs().alias("band"))
              .group_by(["ts", "band"]).agg(pl.col("notional").min().alias("notional"))
              .pivot(on="band", index="ts", values="notional").sort("ts"))
    bands = sorted(float(b) for b in snap.columns if b != "ts")
    if not bands:
        return np.zeros(bars.height)
    joined = bars.select("ts").sort("ts").join_asof(snap, on="ts", strategy="backward")
    out = np.zeros(bars.height)
    levels = np.array([0.0] + bands)
    cols = [joined[str(b) if str(b) in joined.columns else f"{b}"] for b in bands]
    mat = np.column_stack([np.nan_to_num(col.to_numpy().astype(np.float64), nan=0.0) for col in cols])
    for idx in range(bars.height):
        cum = np.concatenate([[0.0], mat[idx]])
        if cum[-1] <= 0:
            continue
        lvl = float(np.interp(ref, cum, levels)) if ref <= cum[-1] else levels[-1] * ref / cum[-1]
        out[idx] = 0.5 * lvl / 100.0
    return out


def _delays(n: int, params: ExecParams, seed_key: int) -> np.ndarray:
    lo, hi = params.exec_delay_min
    lo = int(round(lo * params.delay_mult))
    hi = int(round(hi * params.delay_mult))
    if hi <= lo:
        return np.full(n, lo, dtype=np.int64)
    rng = np.random.default_rng((params.seed, seed_key))
    return rng.integers(lo, hi + 1, size=n).astype(np.int64)


@dataclass
class SymbolTrades:
    """Результат исполнения сигналов одного инструмента."""

    hh_symbol: str
    trades: pl.DataFrame                  # по строке на сигнал (включая неисполненные)
    paths: dict[str, np.ndarray] | None   # p_ts, p_wm_lo, ...; p_start/p_len — в trades
    funding: dict[str, np.ndarray] | None # f_min, f_amt; f_start/f_len — в trades


def execute_signals(bars: pl.DataFrame, signals: pl.DataFrame, params: ExecParams, *,
                    hh_symbol: str, funding: pl.DataFrame | None = None,
                    book_depth: pl.DataFrame | None = None, record_path: bool = True) -> SymbolTrades:
    """Исполнить сигналы одного инструмента на его 1m-барах (ступень A / метки уровня 1).

    bars — load_bars(...) (с m_* колонками, флагами halted/synthetic); signals — схема SIGNALS.
    """
    bars = bars.sort("ts")
    ts = (bars["ts"].dt.epoch("ms").to_numpy() // 60_000).astype(np.int64)
    o = bars["open"].to_numpy().astype(np.float64)
    h = bars["high"].to_numpy().astype(np.float64)
    l = bars["low"].to_numpy().astype(np.float64)
    c = bars["close"].to_numpy().astype(np.float64)

    def mcol(name: str, fallback: np.ndarray) -> np.ndarray:
        if name in bars.columns:
            return bars[name].cast(pl.Float64).fill_null(np.nan).to_numpy().astype(np.float64)
        return np.full(len(fallback), np.nan)

    mo, mh, ml, mc = mcol("m_open", o), mcol("m_high", h), mcol("m_low", l), mcol("m_close", c)
    bad = np.zeros(len(ts), dtype=np.bool_)
    for flag in ("halted", "synthetic"):
        if flag in bars.columns:
            bad |= bars[flag].fill_null(False).to_numpy().astype(np.bool_)
    slip = slippage_fraction(bars, params, book_depth)

    sig = signals.sort("ts")
    n = sig.height
    s_ts = (sig["ts"].dt.epoch("ms").to_numpy() // 60_000).astype(np.int64)
    s_side = sig["side"].to_numpy().astype(np.int64)
    s_limit = (sig["entry_type"] == "limit").to_numpy().astype(np.bool_)
    s_entry = sig["entry_price"].to_numpy().astype(np.float64)
    s_sl = sig["sl"].to_numpy().astype(np.float64)
    s_tp1 = sig["tp1"].to_numpy().astype(np.float64)
    s_tp2 = sig["tp2"].cast(pl.Float64).fill_null(np.nan).to_numpy().astype(np.float64)
    s_frac = sig["tp1_frac"].fill_null(1.0).to_numpy().astype(np.float64)
    s_hold = sig["max_hold_min"].to_numpy().astype(np.int64)
    s_valid = sig["valid_min"].to_numpy().astype(np.int64)
    s_delay = _delays(n, params, zlib.crc32(hh_symbol.encode()))       # стабильный сид (hash() рандомизирован)

    res = _pass1(ts, o, h, l, c, mh, ml, bad, slip, s_ts, s_side, s_limit, s_entry, s_sl, s_tp1, s_tp2,
                 s_frac, s_hold, s_valid, s_delay, params.sl_tp_trigger == "mark", params.be_after_tp1,
                 params.max_entry_wait_min)
    (status, ie, ix, e_min, e_px, e_slip, e_liq, x1_min, x1_px, x1_slip, x1_frac, x1_kind,
     x2_min, x2_px, x2_slip, x2_frac, x2_kind, label, ambiguous, mfe, mae) = res
    final_min = np.where(x2_min >= 0, x2_min, x1_min)
    final_px = np.where(x2_min >= 0, x2_px, x1_px)
    final_kind = np.where(x2_min >= 0, x2_kind, x1_kind).astype(np.int8)
    risk = np.abs(e_px - s_sl)
    with np.errstate(invalid="ignore", divide="ignore"):
        r1 = s_side * (x1_px - e_px) / risk * x1_frac
        r2 = np.where(x2_min >= 0, s_side * (x2_px - e_px) / risk * x2_frac, 0.0)
    r_gross = np.where(status == FILLED, r1 + r2, np.nan)

    paths = fund = None
    p_start = p_len = f_start = f_len = np.zeros(n, dtype=np.int64)
    if record_path:
        p_start, p_len, *parr = _fill_paths(ts, o, h, l, c, mo, mh, ml, mc, bad, status, s_side, ie, ix,
                                            final_px, final_kind)
        paths = dict(zip(["p_ts", "p_wm_lo", "p_wm_hi", "p_mc", "p_wl_lo", "p_wl_hi", "p_lc"], parr))
        if funding is not None and not funding.is_empty():
            f = funding.sort("ts")
            f_ts = (f["ts"].dt.epoch("ms").to_numpy() // 60_000).astype(np.int64)
            f_rate = f["rate"].to_numpy().astype(np.float64)
            f_mark = f["mark_price"].cast(pl.Float64).fill_null(np.nan).to_numpy().astype(np.float64)
            # нет mark на момент начисления — берём close last той минуты
            if np.isnan(f_mark).any():
                idx = np.clip(np.searchsorted(ts, f_ts), 0, len(ts) - 1)
                f_mark = np.where(np.isnan(f_mark), np.where(np.isnan(mc[idx]), c[idx], mc[idx]), f_mark)
            f_start, f_len, f_m, f_a = _fill_funding(status, s_side, e_min, final_min, f_ts, f_rate, f_mark)
        else:
            f_m, f_a = np.zeros(0, np.int64), np.zeros(0)
        fund = {"f_min": f_m, "f_amt": f_a}

    out = pl.DataFrame({
        "hh_symbol": [hh_symbol] * n,
        "signal_ts": sig["ts"],
        "tf": sig["tf"],
        "side": s_side.astype(np.int8),
        "entry_type": sig["entry_type"],
        "score": sig["score"],
        "status": status,
        "delay_min": s_delay,
        "entry_min": e_min,
        "entry_px": e_px,
        "entry_slip": e_slip,
        "entry_liq": e_liq,
        "sl": s_sl,
        "tp1": s_tp1,
        "tp2": s_tp2,
        "tp1_frac": s_frac,
        "risk_unit": np.where(status == FILLED, risk, np.nan),
        "x1_min": x1_min, "x1_px": x1_px, "x1_slip": x1_slip, "x1_frac": x1_frac, "x1_kind": x1_kind,
        "x2_min": x2_min, "x2_px": x2_px, "x2_slip": x2_slip, "x2_frac": x2_frac, "x2_kind": x2_kind,
        "exit_min": np.where(status == FILLED, final_min, -1),
        "exit_kind": final_kind,
        "label": label,
        "ambiguous": ambiguous,
        "r_gross": r_gross,
        "mfe_r": mfe,
        "mae_r": mae,
        "hold_min": np.where(status == FILLED, final_min - e_min, 0),
        "p_start": p_start, "p_len": p_len,
        "f_start": f_start, "f_len": f_len,
    })
    comp_cols = [col for col in sig.columns if col.startswith("c_")]
    if comp_cols:
        out = pl.concat([out, sig.select(comp_cols)], how="horizontal")
    return SymbolTrades(hh_symbol=hh_symbol, trades=out, paths=paths, funding=fund)


def fees_r(trades: pl.DataFrame, maker: float, taker: float) -> pl.Series:
    """Комиссии сделки в R (на 1 единицу риска): вход (maker/taker) + выходы (taker)."""
    entry_fee = pl.when(pl.col("entry_liq") == 0).then(maker).otherwise(taker) * pl.col("entry_px")
    exit_fee = taker * (pl.col("x1_px") * pl.col("x1_frac")
                        + pl.when(pl.col("x2_min") >= 0).then(pl.col("x2_px") * pl.col("x2_frac")).otherwise(0.0))
    return trades.select(((entry_fee + exit_fee) / pl.col("risk_unit")).alias("fees_r")).to_series()
