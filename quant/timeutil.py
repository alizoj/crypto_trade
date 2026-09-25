"""Время: всё в UTC. Внутри движка — целые минуты (или миллисекунды) с эпохи.

`ts` свечи = время её открытия (раздел 3 ТЗ). Бар ТФ считается закрытым в ts + tf.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

MS_PER_MIN = 60_000
MIN_PER_DAY = 1440

_TF = {"1m": 1, "3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "2h": 120, "4h": 240, "1d": 1440}


def tf_minutes(tf: str) -> int:
    try:
        return _TF[tf]
    except KeyError:
        raise ValueError(f"неизвестный таймфрейм {tf!r}") from None


def utc(y: int, m: int, d: int, hh: int = 0, mm: int = 0, ss: int = 0) -> datetime:
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_ms(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc)


def to_min(dt: datetime) -> int:
    return to_ms(dt) // MS_PER_MIN


def from_min(m: int) -> datetime:
    return from_ms(int(m) * MS_PER_MIN)


def parse_dt(value: str | datetime | None) -> datetime | None:
    """'2024-01', '2024-01-05', '2024-01-05T10:00', ISO с Z — всё в UTC."""
    if value is None or isinstance(value, datetime):
        return value if (value is None or value.tzinfo) else value.replace(tzinfo=timezone.utc)
    s = value.strip().replace("Z", "+00:00")
    for fmt in ("%Y-%m", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def prop_day_id(ts_min: int, boundary_min: int) -> int:
    """Номер «проп-дня»: день начинается в boundary_min (минуты от 00:00 UTC).

    Для Crypto+TradFi граница 20:00 UTC (= 00:00 UTC+4): 20:00 UTC уже новый день.
    """
    return (int(ts_min) + (MIN_PER_DAY - boundary_min)) // MIN_PER_DAY


def prop_day_start_min(day_id: int, boundary_min: int) -> int:
    return day_id * MIN_PER_DAY - (MIN_PER_DAY - boundary_min)


def next_reset_min(ts_min: int, boundary_min: int) -> int:
    return prop_day_start_min(prop_day_id(ts_min, boundary_min) + 1, boundary_min)


def minute_of_day(ts_min: int) -> int:
    return int(ts_min) % MIN_PER_DAY


def floor_tf_min(ts_min: int, tf: str, offset_min: int = 0) -> int:
    n = tf_minutes(tf)
    return ((int(ts_min) - offset_min) // n) * n + offset_min


def month_range(start: datetime, end: datetime) -> list[str]:
    """Список 'YYYY-MM' от start до end включительно."""
    out = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append(f"{y:04d}-{m:02d}")
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out


def day_range(start: datetime, end: datetime) -> list[str]:
    out = []
    d = start.date()
    while d <= end.date():
        out.append(d.isoformat())
        d += timedelta(days=1)
    return out
