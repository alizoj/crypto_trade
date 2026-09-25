"""Календарь сессий базового рынка для TradFi-перпов (правило 3.1.6).

По `underlying_type` из exchangeInfo Binance:
- COIN — 24/7, сессия всегда открыта;
- EQUITY — NYSE/NASDAQ (XNYS), регулярная сессия, праздники и переход на зимнее время учитывает exchange_calendars;
- KR_EQUITY — KRX (XKRX);
- COMMODITY — CME Globex (CMES), почти 24/5 с перерывом;
- PREMARKET — базового рынка нет: сессия считается закрытой (сигналы выключены флагом
  tradfi_off_session_signals), # ASSUMPTION.
"""

from __future__ import annotations

from datetime import datetime, timezone
from functools import lru_cache

import numpy as np
import pandas as pd

_CAL = {"EQUITY": "XNYS", "KR_EQUITY": "XKRX", "COMMODITY": "CMES"}


@lru_cache(maxsize=8)
def _calendar(code: str):
    import exchange_calendars as xc

    return xc.get_calendar(code)


def is_open(underlying_type: str, ts: datetime) -> bool:
    if underlying_type == "COIN":
        return True
    if underlying_type == "PREMARKET" or underlying_type not in _CAL:
        return False
    cal = _calendar(_CAL[underlying_type])
    t = pd.Timestamp(ts.astimezone(timezone.utc)).floor("min")
    if t < cal.first_minute or t > cal.last_minute:
        return False
    return bool(cal.is_open_on_minute(t, ignore_breaks=False))


def open_mask(underlying_type: str, ts_ms: np.ndarray) -> np.ndarray:
    """Векторная версия: для массива ts (мс, UTC) — открыта ли сессия."""
    ts_ms = np.asarray(ts_ms, dtype=np.int64)
    if underlying_type == "COIN":
        return np.ones(len(ts_ms), dtype=bool)
    if underlying_type == "PREMARKET" or underlying_type not in _CAL:
        return np.zeros(len(ts_ms), dtype=bool)
    if len(ts_ms) == 0:
        return np.zeros(0, dtype=bool)
    cal = _calendar(_CAL[underlying_type])
    lo = max(pd.Timestamp(int(ts_ms.min()), unit="ms", tz="UTC"), cal.first_minute)
    hi = min(pd.Timestamp(int(ts_ms.max()), unit="ms", tz="UTC"), cal.last_minute)
    if lo > hi:
        return np.zeros(len(ts_ms), dtype=bool)
    # Интервалы сессий: open..close (с учётом перерывов)
    sessions = cal.sessions_in_range(lo.normalize().tz_localize(None) - pd.Timedelta(days=1),
                                     hi.normalize().tz_localize(None) + pd.Timedelta(days=1))
    opens = cal.opens.reindex(sessions).to_numpy()
    closes = cal.closes.reindex(sessions).to_numpy()
    bstart = cal.break_starts.reindex(sessions).to_numpy()
    bend = cal.break_ends.reindex(sessions).to_numpy()
    intervals: list[tuple[int, int]] = []
    for o, c, bs, be in zip(opens, closes, bstart, bend):
        o_ms = pd.Timestamp(o).value // 1_000_000
        c_ms = pd.Timestamp(c).value // 1_000_000
        if pd.isna(bs) or pd.isna(be):
            intervals.append((o_ms, c_ms))
        else:
            bs_ms = pd.Timestamp(bs).value // 1_000_000
            be_ms = pd.Timestamp(be).value // 1_000_000
            intervals.append((o_ms, bs_ms))
            intervals.append((be_ms, c_ms))
    if not intervals:
        return np.zeros(len(ts_ms), dtype=bool)
    starts = np.array([a for a, _ in intervals], dtype=np.int64)
    ends = np.array([b for _, b in intervals], dtype=np.int64)
    order = np.argsort(starts)
    starts, ends = starts[order], ends[order]
    idx = np.searchsorted(starts, ts_ms, side="right") - 1
    ok = idx >= 0
    res = np.zeros(len(ts_ms), dtype=bool)
    res[ok] = ts_ms[ok] < ends[idx[ok]]
    return res


def next_change(underlying_type: str, ts: datetime) -> datetime | None:
    """Время следующего открытия/закрытия сессии (для UI)."""
    if underlying_type == "COIN" or underlying_type not in _CAL:
        return None
    cal = _calendar(_CAL[underlying_type])
    t = pd.Timestamp(ts.astimezone(timezone.utc)).floor("min")
    try:
        if cal.is_open_on_minute(t):
            nxt = cal.next_close(t)
        else:
            nxt = cal.next_open(t)
    except Exception:
        return None
    return nxt.to_pydatetime()
