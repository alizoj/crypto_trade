"""Интерфейс стратегии (раздел 6 ТЗ).

Стратегия считает всё векторно по закрытым барам (`compute`), а `on_bar(ctx)` — тот же
расчёт на окне истории до текущего бара включительно. Так бэктест и live дают одинаковые
сигналы на одних и тех же барах (приёмка фазы 6).

Контракт `compute` → «кадр оценок» (по одной строке на закрытый бар сигнального ТФ):
    ts            — время открытия бара (UTC)
    close_ts      — время закрытия бара = момент, когда значения известны
    score_long, score_short — 0..100
    signal_long, signal_short — bool: порог пройден и фильтры разрешают
    sl_long, tp1_long, tp2_long, sl_short, tp1_short, tp2_short — уровни (float, может быть null)
    entry_long, entry_short — цена входа (для market = close бара)
    c_<фактор>_long, c_<фактор>_short — вклад фактора в баллы
    + любые индикаторные колонки для скринера (rsi14, adx, atr_pct, vwap_dist_pct, htf_trend, cvd_trend)
`signals_from(frame)` превращает кадр оценок в таблицу SIGNALS (quant.schemas).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import polars as pl

from quant.schemas import SIGNALS, conform

Side = Literal["long", "short"]


@dataclass(frozen=True)
class Factor:
    name: str             # машинное имя, напр. "htf_trend"
    label: str            # человекочитаемое, напр. "HTF аптренд (EMA200 4H)"
    value: float | str | None
    points: float         # вклад в баллы
    max_points: float
    verdict: Literal["pro", "con", "neutral"]


@dataclass(frozen=True)
class Signal:
    ts: datetime                      # закрытие бара сигнала
    hh_symbol: str
    tf: str
    side: Side
    entry_type: Literal["market", "limit"]
    entry_price: float
    sl: float
    tp1: float
    tp2: float | None
    tp1_frac: float
    score: float
    max_hold_min: int
    valid_min: int
    sl_type: Literal["stop_market"] = "stop_market"
    sl_trigger: Literal["last", "mark"] = "last"
    components: dict[str, float] = field(default_factory=dict)
    reason: dict[str, list[str]] = field(default_factory=dict)   # {"pros": [...], "cons": [...]}


class Context:
    """Доступ стратегии к данным — только до текущего бара включительно.

    bars    — закрытые бары сигнального ТФ (последний = текущий).
    htf     — закрытые бары старшего ТФ к моменту закрытия текущего бара.
    bars_1m — 1m-бары до момента закрытия текущего бара (для VWAP/CVD и т.п.).
    """

    def __init__(self, hh_symbol: str, tf: str, bars: pl.DataFrame, htf: pl.DataFrame | None = None,
                 bars_1m: pl.DataFrame | None = None, meta: dict[str, Any] | None = None):
        self.hh_symbol = hh_symbol
        self.tf = tf
        self._bars = bars
        self._htf = htf
        self._bars_1m = bars_1m
        self.meta = meta or {}
        if not bars.is_empty():
            self.now_close_ms = int(bars["close_ts"][-1].timestamp() * 1000) if "close_ts" in bars.columns else None
        else:
            self.now_close_ms = None

    @property
    def bars(self) -> pl.DataFrame:
        return self._bars

    @property
    def htf(self) -> pl.DataFrame | None:
        return self._htf

    @property
    def bars_1m(self) -> pl.DataFrame | None:
        return self._bars_1m


class Strategy(ABC):
    name: str = "base"

    def __init__(self, params: dict[str, Any] | None = None):
        self.params = params or {}

    @abstractmethod
    def compute(self, bars_tf: pl.DataFrame, *, hh_symbol: str, tf: str,
                htf: pl.DataFrame | None = None, bars_1m: pl.DataFrame | None = None,
                meta: dict[str, Any] | None = None) -> pl.DataFrame:
        """Кадр оценок по всем закрытым барам (см. docstring модуля)."""

    @abstractmethod
    def explain(self, row: dict[str, Any], side: Side) -> list[Factor]:
        """Разбор score: таблица факторов для строки кадра оценок."""

    def signals_from(self, frame: pl.DataFrame, *, hh_symbol: str, tf: str) -> pl.DataFrame:
        """Кадр оценок → SIGNALS. Общая реализация для всех стратегий."""
        hold = int(self.params.get("max_hold_min", 24 * 60))
        valid = int(self.params.get("valid_min", 60))
        entry_type = self.params.get("entry_type", "market")
        tp1_frac = float(self.params.get("tp1_frac", 1.0))
        parts = []
        for side, sgn in (("long", 1), ("short", -1)):
            col = f"signal_{side}"
            if col not in frame.columns:
                continue
            sub = frame.filter(pl.col(col))
            if sub.is_empty():
                continue
            parts.append(sub.select(
                pl.col("close_ts").alias("ts"),
                pl.lit(hh_symbol).alias("hh_symbol"),
                pl.lit(tf).alias("tf"),
                pl.lit(sgn, dtype=pl.Int8).alias("side"),
                pl.lit(entry_type).alias("entry_type"),
                pl.col(f"entry_{side}").alias("entry_price"),
                pl.col(f"sl_{side}").alias("sl"),
                pl.col(f"tp1_{side}").alias("tp1"),
                (pl.col(f"tp2_{side}") if f"tp2_{side}" in frame.columns else pl.lit(None, dtype=pl.Float64)).alias("tp2"),
                pl.lit(tp1_frac).alias("tp1_frac"),
                pl.col(f"score_{side}").alias("score"),
                pl.lit(hold, dtype=pl.Int32).alias("max_hold_min"),
                pl.lit(valid, dtype=pl.Int32).alias("valid_min"),
                *[pl.col(c).alias("c_" + c[2:-len(side) - 1]) for c in frame.columns
                  if c.startswith("c_") and c.endswith("_" + side)],
            ))
        if not parts:
            return conform(pl.DataFrame(), SIGNALS)
        out = pl.concat(parts, how="diagonal_relaxed").sort("ts")
        out = out.with_columns(
            pl.when(pl.col("tp2").is_null()).then(pl.lit(1.0)).otherwise(pl.col("tp1_frac")).alias("tp1_frac")
        )
        return conform(out, SIGNALS)

    def on_bar(self, ctx: Context) -> list[Signal]:
        """Live-интерфейс: сигналы на последнем закрытом баре окна ctx."""
        frame = self.compute(ctx.bars, hh_symbol=ctx.hh_symbol, tf=ctx.tf, htf=ctx.htf,
                             bars_1m=ctx.bars_1m, meta=ctx.meta)
        if frame.is_empty():
            return []
        last = frame.tail(1)
        sigs = self.signals_from(last, hh_symbol=ctx.hh_symbol, tf=ctx.tf)
        out: list[Signal] = []
        row0 = last.row(0, named=True)
        for r in sigs.iter_rows(named=True):
            side: Side = "long" if r["side"] == 1 else "short"
            factors = self.explain(row0, side)
            out.append(Signal(
                ts=r["ts"], hh_symbol=r["hh_symbol"], tf=r["tf"], side=side,
                entry_type=r["entry_type"], entry_price=r["entry_price"], sl=r["sl"], tp1=r["tp1"],
                tp2=r["tp2"], tp1_frac=r["tp1_frac"], score=r["score"], max_hold_min=r["max_hold_min"],
                valid_min=r["valid_min"],
                sl_trigger=self.params.get("sl_trigger", "last"),
                components={k: v for k, v in r.items() if k.startswith("c_")},
                reason={"pros": [f.label for f in factors if f.verdict == "pro"],
                        "cons": [f.label for f in factors if f.verdict == "con"]},
            ))
        return out
