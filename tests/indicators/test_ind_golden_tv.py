"""Golden-тесты: паритет индикаторов с TradingView (раздел 6 ТЗ).

Фикстуры — CSV «Export chart data» с графика BINANCE:<SYMBOL>.P и скриптом golden_export
(см. tests/indicators/golden/README.md). Пока файлов нет — тесты пропускаются.

Сравнение: относительный допуск 1e-6 после прогрева ≥ 25×period (для RMA-индикаторов 10×period
мало: (13/14)^140 ≈ 3e-5). Отдельный тест сверяет число ведущих na — он решает вопрос seed
у ta.ema (SMA-seed встроенного ta.ema против pine_ema из справочника) и прогрев DMI.
"""

from __future__ import annotations

import csv
import math
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pytest

from quant.indicators import (
    atr, bb, change, dmi, ema, obv, pivothigh, pivotlow, rma, rsi, session_key, sma, stdev, tr, vwap, wma,
)
from quant.indicators.core import hlc3

GOLDEN_DIR = Path(__file__).parent / "golden"
FILES = sorted(GOLDEN_DIR.glob("*.csv"))
SKIP_REASON = ("нет CSV-фикстур TradingView в tests/indicators/golden/ — как экспортировать, "
               "см. tests/indicators/golden/README.md")
RTOL = 1e-6
WARMUP_MULT = 25

Data = dict[str, np.ndarray]


def _dmi(d: Data, k: int) -> np.ndarray:
    return dmi(d["high"], d["low"], d["close"], 14, 14)[k]


def _bb(d: Data, k: int) -> np.ndarray:
    return bb(d["close"], 20, 2.0)[k]


# колонка CSV → (период для прогрева, расчёт)
SPECS: dict[str, tuple[int, Callable[[Data], np.ndarray]]] = {
    "sma_20": (20, lambda d: sma(d["close"], 20)),
    "ema_20": (20, lambda d: ema(d["close"], 20)),
    "ema_200": (200, lambda d: ema(d["close"], 200)),
    "rma_14": (14, lambda d: rma(d["close"], 14)),
    "wma_20": (20, lambda d: wma(d["close"], 20)),
    "stdev_20": (20, lambda d: stdev(d["close"], 20)),
    "rsi_14": (14, lambda d: rsi(d["close"], 14)),
    "tr": (1, lambda d: tr(d["high"], d["low"], d["close"], True)),
    "atr_14": (14, lambda d: atr(d["high"], d["low"], d["close"], 14)),
    "plus_di_14": (28, lambda d: _dmi(d, 0)),
    "minus_di_14": (28, lambda d: _dmi(d, 1)),
    "adx_14": (28, lambda d: _dmi(d, 2)),
    "bb_basis_20": (20, lambda d: _bb(d, 0)),
    "bb_upper_20": (20, lambda d: _bb(d, 1)),
    "bb_lower_20": (20, lambda d: _bb(d, 2)),
    "change_1": (1, lambda d: change(d["close"], 1)),
    "obv": (1, lambda d: obv(d["close"], d["volume"])),
    "vwap": (1, lambda d: vwap(hlc3(d["high"], d["low"], d["close"]), d["volume"],
                               session_key(d["ts"], "utc_day"))),
}
# ведущие na должны совпасть 1:1 (TradingView считает с первого бара графика)
WARMUP_PATTERN = ("sma_20", "ema_20", "ema_200", "rma_14", "rsi_14", "atr_14", "adx_14", "plus_di_14")


def _float(s: str) -> float:
    s = (s or "").strip()
    if s in ("", "NaN", "nan", "n/a", "∅"):
        return math.nan
    return float(s)


def _time_ms(s: str) -> int:
    s = s.strip()
    try:
        v = float(s)
        return int(v if v > 1e11 else v * 1000)          # UNIX-секунды или миллисекунды
    except ValueError:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        dt = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)


def load_golden(path: Path) -> Data:
    """CSV «Export chart data» → массивы; имена колонок в нижнем регистре, дубли — первый."""
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    header = [h.strip().lower() for h in rows[0]]
    cols: dict[str, int] = {}
    for i, h in enumerate(header):
        cols.setdefault(h, i)
    body = [r for r in rows[1:] if r]
    out: Data = {"ts": np.array([_time_ms(r[cols["time"]]) for r in body], dtype=np.int64)}
    for name, i in cols.items():
        if name != "time":
            out[name] = np.array([_float(r[i]) if i < len(r) else math.nan for r in body])
    for req in ("open", "high", "low", "close"):
        if req not in out:
            raise ValueError(f"{path.name}: нет колонки {req}")
    return out


@pytest.fixture(params=FILES or [None], ids=lambda p: p.name if p else "no-fixtures")
def golden(request) -> Data:
    if request.param is None:
        pytest.skip(SKIP_REASON)
    return load_golden(request.param)


def _leading_nan(x: np.ndarray) -> int:
    ok = np.flatnonzero(~np.isnan(x))
    return int(ok[0]) if len(ok) else len(x)


def check_column(d: Data, col: str) -> str | None:
    """None — совпало; иначе текст ошибки. Сравнение после прогрева WARMUP_MULT×period."""
    period, fn = SPECS[col]
    ours, tv = fn(d), d[col]
    start = WARMUP_MULT * period
    o, t = ours[start:], tv[start:]
    both = ~np.isnan(t)
    if np.isnan(o[both]).any():
        return f"{col}: у нас na там, где у TradingView значение"
    scale = float(np.nanmedian(np.abs(t))) if both.any() else 1.0
    bad = ~np.isclose(o[both], t[both], rtol=RTOL, atol=1e-9 * (scale or 1.0))
    if bad.any():
        i = int(np.flatnonzero(both)[np.flatnonzero(bad)[0]]) + start
        return f"{col}: {int(bad.sum())} расхождений > {RTOL}, первое на баре {i}: ours={ours[i]!r} tv={tv[i]!r}"
    return None


@pytest.mark.parametrize("col", list(SPECS))
def test_golden_values(golden: Data, col: str):
    if col not in golden:
        pytest.skip(f"в CSV нет колонки {col}")
    if col in ("obv", "vwap") and "volume" not in golden:
        pytest.skip("в CSV нет колонки volume")
    if len(golden[col]) <= WARMUP_MULT * SPECS[col][0]:
        pytest.skip(f"мало баров для прогрева {WARMUP_MULT}×{SPECS[col][0]}")
    err = check_column(golden, col)
    if err:
        pytest.fail(err)


def test_harness_selfcheck(tmp_path):
    """Харнесс работает без фикстур: CSV из наших значений проходит, искажённый — нет."""
    from quant.indicators.synthetic import random_walk_bars

    df = random_walk_bars(6000, tf="1h", seed=11)
    base: Data = {k: df[k].to_numpy() for k in ("open", "high", "low", "close", "volume")}
    base["ts"] = df["ts"].dt.epoch("ms").to_numpy()
    header = ["time", "open", "high", "low", "close", "Volume"] + list(SPECS)
    vals = {c: SPECS[c][1](base) for c in SPECS}
    path = tmp_path / "BINANCE_TEST.P_60.csv"
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        for i in range(len(base["ts"])):
            row = [str(base["ts"][i] // 1000)] + [repr(float(base[k][i])) for k in ("open", "high", "low", "close",
                                                                                     "volume")]
            row += ["" if np.isnan(vals[c][i]) else repr(float(vals[c][i])) for c in SPECS]
            w.writerow(row)
    d = load_golden(path)
    assert np.array_equal(d["ts"], base["ts"])
    for col in SPECS:
        assert check_column(d, col) is None, col
        assert _leading_nan(SPECS[col][1](d)) == _leading_nan(d[col])
    d["rsi_14"][-1] *= 1 + 1e-5
    assert check_column(d, "rsi_14") is not None


def test_golden_warmup_pattern(golden: Data):
    """Число ведущих na = у TradingView (решает seed ta.ema и прогрев DMI/RSI)."""
    checked = 0
    for col in WARMUP_PATTERN:
        if col in golden:
            assert _leading_nan(SPECS[col][1](golden)) == _leading_nan(golden[col]), col
            checked += 1
    if not checked:
        pytest.skip("в CSV нет колонок для проверки прогрева")


@pytest.mark.parametrize("col,fn,src", [("ph_5_5", pivothigh, "high"), ("pl_5_5", pivotlow, "low")])
def test_golden_pivots(golden: Data, col, fn, src):
    """ta.pivothigh/ta.pivotlow(5, 5): значение на баре подтверждения; проверяет и правило равенств."""
    if col not in golden:
        pytest.skip(f"в CSV нет колонки {col}")
    vals, _ = fn(golden[src], 5, 5)
    tv = golden[col]
    np.testing.assert_array_equal(np.isnan(vals), np.isnan(tv))
    ok = ~np.isnan(tv)
    np.testing.assert_allclose(vals[ok], tv[ok], rtol=RTOL)
