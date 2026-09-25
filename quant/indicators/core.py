"""Базовые индикаторы с семантикой Pine Script v6 (ta.*), раздел 6 ТЗ.

Вход — numpy-массивы (приводятся к float64), выход — float64 той же длины; NaN там, где Pine даёт na.
Рекурсии и скользящие окна — numba (@njit(cache=True)).

Обработка na (# ASSUMPTION, окончательно решают golden-тесты): как в справочнике Pine для
ta.sma/ta.ema/ta.rma/ta.wma/ta.stdev — «na values in the source series are ignored; the function
calculates on the length quantity of non-na values». Бар с NaN на входе не меняет состояние,
на выходе этого бара NaN. Ведущие NaN (прогрев производных рядов: RSI, DMI) при любой трактовке
дают одинаковый результат.

Выбор seed для ta.ema: справочник Pine приводит `pine_ema`, который сидирует первым значением src,
но встроенный ta.ema на графике даёт na первые length-1 баров и стартует с SMA — как ta.rma.
Реализован встроенный вариант (`seed="sma"`), `seed="first"` — вариант pine_ema из справочника.
Какой из них у TradingView, проверяет test_golden_warmup_pattern (число ведущих na).
"""

from __future__ import annotations

import numpy as np
from numba import njit

# Порог обнуления отклонений в ta.stdev (функция SUM из справочника Pine).
STDEV_EPS = 1e-10


def f64(x) -> np.ndarray:
    return np.ascontiguousarray(x, dtype=np.float64)


# ---------------------------------------------------------------- numba-ядра


@njit(cache=True)
def _sma_nb(x, n):
    m = len(x)
    out = np.full(m, np.nan)
    buf = np.zeros(n)
    cnt = 0
    pos = 0
    for i in range(m):
        v = x[i]
        if np.isnan(v):
            continue
        buf[pos] = v
        pos += 1
        if pos == n:
            pos = 0
        if cnt < n:
            cnt += 1
        if cnt == n:
            s = 0.0
            for k in range(n):          # сумма окна заново: без накопления ошибки округления
                s += buf[(pos + k) % n]
            out[i] = s / n
    return out


@njit(cache=True)
def _ema_nb(x, n, alpha, sma_seed):
    m = len(x)
    out = np.full(m, np.nan)
    prev = np.nan
    cnt = 0
    s = 0.0
    for i in range(m):
        v = x[i]
        if np.isnan(v):
            continue
        if np.isnan(prev):
            if sma_seed:
                s += v
                cnt += 1
                if cnt == n:
                    prev = s / n
                    out[i] = prev
            else:
                prev = v
                out[i] = prev
        else:
            prev = alpha * v + (1.0 - alpha) * prev
            out[i] = prev
    return out


@njit(cache=True)
def _wma_nb(x, n):
    m = len(x)
    out = np.full(m, np.nan)
    buf = np.zeros(n)
    cnt = 0
    pos = 0
    norm = n * (n + 1) / 2.0
    for i in range(m):
        v = x[i]
        if np.isnan(v):
            continue
        buf[pos] = v
        pos += 1
        if pos == n:
            pos = 0
        if cnt < n:
            cnt += 1
        if cnt == n:
            s = 0.0
            for k in range(n):          # k=0 — самый свежий, вес n
                s += buf[(pos - 1 - k) % n] * (n - k)
            out[i] = s / norm
    return out


@njit(cache=True)
def _stdev_nb(x, n, biased, eps):
    m = len(x)
    out = np.full(m, np.nan)
    buf = np.zeros(n)
    cnt = 0
    pos = 0
    for i in range(m):
        v = x[i]
        if np.isnan(v):
            continue
        buf[pos] = v
        pos += 1
        if pos == n:
            pos = 0
        if cnt < n:
            cnt += 1
        if cnt == n:
            s = 0.0
            for k in range(n):
                s += buf[(pos + k) % n]
            avg = s / n
            ss = 0.0
            for k in range(n):
                d = buf[(pos + k) % n] - avg
                if abs(d) <= eps:
                    d = 0.0
                ss += d * d
            den = n if biased else n - 1
            out[i] = np.sqrt(ss / den) if den > 0 else np.nan
    return out


@njit(cache=True)
def _rolling_sum_nb(x, n):
    # Сумма последних n позиций (NaN, если окно неполное или в нём есть NaN) — как math.sum.
    m = len(x)
    out = np.full(m, np.nan)
    for i in range(n - 1, m):
        s = 0.0
        ok = True
        for k in range(i - n + 1, i + 1):
            v = x[k]
            if np.isnan(v):
                ok = False
                break
            s += v
        if ok:
            out[i] = s
    return out


@njit(cache=True)
def _rolling_ext_nb(x, n, is_max):
    # ta.highest/ta.lowest по последним n позициям, NaN внутри окна пропускаются.
    m = len(x)
    out = np.full(m, np.nan)
    for i in range(n - 1, m):
        best = np.nan
        for k in range(i - n + 1, i + 1):
            v = x[k]
            if np.isnan(v):
                continue
            if np.isnan(best) or (v > best if is_max else v < best):
                best = v
        out[i] = best
    return out


@njit(cache=True)
def _fixnan_nb(x):
    out = np.empty_like(x)
    last = np.nan
    for i in range(len(x)):
        if not np.isnan(x[i]):
            last = x[i]
        out[i] = last
    return out


@njit(cache=True)
def _barssince_nb(cond):
    out = np.full(len(cond), np.nan)
    last = -1
    for i in range(len(cond)):
        if cond[i]:
            last = i
        if last >= 0:
            out[i] = i - last
    return out


@njit(cache=True)
def _valuewhen_nb(cond, src, occurrence):
    m = len(cond)
    out = np.full(m, np.nan)
    hist = np.full(occurrence + 1, np.nan)   # hist[0] — последнее значение
    cnt = 0
    for i in range(m):
        if cond[i]:
            for k in range(occurrence, 0, -1):
                hist[k] = hist[k - 1]
            hist[0] = src[i]
            cnt += 1
        if cnt > occurrence:
            out[i] = hist[occurrence]
    return out


# ---------------------------------------------------------------- публичные функции


def sma(x, n: int) -> np.ndarray:
    """ta.sma: среднее последних n не-na значений; na до n значений."""
    return _sma_nb(f64(x), int(n))


def ema(x, n: int, seed: str = "sma") -> np.ndarray:
    """ta.ema: alpha = 2/(n+1). seed="sma" — встроенный ta.ema (na первые n-1 баров, старт с SMA);
    seed="first" — pine_ema из справочника (старт с первого значения)."""
    if seed not in ("sma", "first"):
        raise ValueError(f"seed: sma | first, получено {seed!r}")
    return _ema_nb(f64(x), int(n), 2.0 / (n + 1.0), seed == "sma")


def rma(x, n: int) -> np.ndarray:
    """ta.rma (Wilder): alpha = 1/n, первое значение = SMA первых n значений, до этого na."""
    return _ema_nb(f64(x), int(n), 1.0 / n, True)


def wma(x, n: int) -> np.ndarray:
    """ta.wma: веса n..1 (самый свежий бар — вес n)."""
    return _wma_nb(f64(x), int(n))


def stdev(x, n: int, biased: bool = True) -> np.ndarray:
    """ta.stdev(biased=true) — популяционное СКО; отклонения |d| <= 1e-10 обнуляются, как в Pine."""
    return _stdev_nb(f64(x), int(n), bool(biased), STDEV_EPS)


def rolling_sum(x, n: int) -> np.ndarray:
    """math.sum: сумма последних n баров (na, если окно неполное или есть na)."""
    return _rolling_sum_nb(f64(x), int(n))


def highest(x, n: int) -> np.ndarray:
    """ta.highest(source, n): максимум последних n баров, включая текущий."""
    return _rolling_ext_nb(f64(x), int(n), True)


def lowest(x, n: int) -> np.ndarray:
    return _rolling_ext_nb(f64(x), int(n), False)


def change(x, length: int = 1) -> np.ndarray:
    """ta.change: x - x[length]."""
    x = f64(x)
    return x - shift(x, length)


def fixnan(x) -> np.ndarray:
    """fixnan: na заменяется последним не-na значением."""
    return _fixnan_nb(f64(x))


def nz(x, value: float = 0.0) -> np.ndarray:
    x = f64(x)
    return np.where(np.isnan(x), value, x)


def barssince(cond) -> np.ndarray:
    """ta.barssince: сколько баров назад cond был true (0 — на текущем), NaN — ни разу."""
    return _barssince_nb(np.ascontiguousarray(cond, dtype=np.bool_))


def valuewhen(cond, src, occurrence: int = 0) -> np.ndarray:
    """ta.valuewhen: значение src на occurrence-м (0 — последнем) баре, где cond был true."""
    return _valuewhen_nb(np.ascontiguousarray(cond, dtype=np.bool_), f64(src), int(occurrence))


def shift(x, k: int = 1, fill=None) -> np.ndarray:
    """x[k] в терминах Pine (значение k баров назад). Заполнение: NaN для float, False для bool."""
    x = np.asarray(x)
    if fill is None:
        fill = False if x.dtype == np.bool_ else (0 if x.dtype.kind in "iu" else np.nan)
    out = np.full(len(x), fill, dtype=x.dtype)
    if k == 0:
        out[:] = x
    elif 0 < k < len(x):
        out[k:] = x[:-k]
    return out


def hlc3(high, low, close) -> np.ndarray:
    return (f64(high) + f64(low) + f64(close)) / 3.0


def tr(high, low, close, handle_na: bool = True) -> np.ndarray:
    """ta.tr(handle_na): max(high-low, |high-close[1]|, |low-close[1]|).
    handle_na=True (ta.tr(true)): при na close[1] (первый бар) → high-low; False → na."""
    h, lo, c = f64(high), f64(low), f64(close)
    pc = shift(c, 1)
    a = h - lo
    out = np.maximum(a, np.maximum(np.abs(h - pc), np.abs(lo - pc)))
    if handle_na:
        out = np.where(np.isnan(pc), a, out)
    return out


def atr(high, low, close, n: int = 14) -> np.ndarray:
    """ta.atr(n) = ta.rma(ta.tr(true), n)."""
    return rma(tr(high, low, close, True), n)


def rsi(x, n: int = 14) -> np.ndarray:
    """ta.rsi (Wilder): u=max(x-x[1],0), d=max(x[1]-x,0), rs=rma(u)/rma(d).
    Как во встроенном индикаторе RSI: rma(d)=0 → 100, rma(u)=0 → 0."""
    ch = change(x, 1)
    nan = np.isnan(ch)
    u = np.where(nan, np.nan, np.maximum(ch, 0.0))
    d = np.where(nan, np.nan, np.maximum(-ch, 0.0))
    up = rma(u, n)
    dn = rma(d, n)
    with np.errstate(divide="ignore", invalid="ignore"):
        val = 100.0 - 100.0 / (1.0 + up / dn)
    return np.where(dn == 0, 100.0, np.where(up == 0, 0.0, val))


def dmi(high, low, close, di_len: int = 14, adx_len: int = 14) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """ta.dmi(diLength, adxSmoothing) → (+DI, -DI, ADX) по коду из справочника Pine:
    up=change(high), down=-change(low); plusDM = up>down and up>0 ? up : 0 (na на первом баре);
    trur = rma(ta.tr, len) — ta.tr без handle_na, т.е. na на первом баре (как в справочнике);
    plus = fixnan(100*rma(plusDM)/trur); adx = 100*rma(|plus-minus|/(sum==0 ? 1 : sum), adxLen).
    Деление на trur=0 даёт na (затем fixnan) — # ASSUMPTION для плоских рядов."""
    h, lo, c = f64(high), f64(low), f64(close)
    up = change(h, 1)
    down = -change(lo, 1)
    plus_dm = np.where(np.isnan(up), np.nan, np.where((up > down) & (up > 0), up, 0.0))
    minus_dm = np.where(np.isnan(down), np.nan, np.where((down > up) & (down > 0), down, 0.0))
    trur = rma(tr(h, lo, c, False), di_len)
    trur_safe = np.where(trur == 0, np.nan, trur)
    with np.errstate(divide="ignore", invalid="ignore"):
        plus = fixnan(100.0 * rma(plus_dm, di_len) / trur_safe)
        minus = fixnan(100.0 * rma(minus_dm, di_len) / trur_safe)
        s = plus + minus
        dx = np.abs(plus - minus) / np.where(s == 0, 1.0, s)
    adx = 100.0 * rma(dx, adx_len)
    return plus, minus, adx


def adx(high, low, close, di_len: int = 14, adx_len: int = 14) -> np.ndarray:
    return dmi(high, low, close, di_len, adx_len)[2]


def bb(x, n: int = 20, mult: float = 2.0) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """ta.bb → (basis, upper, lower): basis = sma, dev = mult * stdev(biased)."""
    basis = sma(x, n)
    dev = mult * stdev(x, n, True)
    return basis, basis + dev, basis - dev


def obv(close, volume) -> np.ndarray:
    """ta.obv = ta.cum(math.sign(ta.change(close)) * volume); na (первый бар) считается 0."""
    val = np.sign(change(close, 1)) * f64(volume)
    return np.cumsum(np.where(np.isnan(val), 0.0, val))
