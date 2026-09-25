"""Модели PostgreSQL (локально — SQLite с той же схемой).

Журналы, конфиги, прогоны, реестр испытаний, модели калибровки, манифест архива, реестр гэпов.
Сами ряды данных живут в Parquet/ClickHouse, не здесь.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, BigInteger, Boolean, DateTime, Float, ForeignKey, Index, Integer,
                        String, Text, UniqueConstraint)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


# ---------------------------------------------------------------- данные (фаза 2)

class ArchiveFile(Base):
    """Манифест загруженных файлов архива (3.2.4)."""

    __tablename__ = "archive_files"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32))              # binance_um | bybit_linear | binance_spot
    dataset: Mapped[str] = mapped_column(String(64))             # klines_1m | markPriceKlines_1m | fundingRate | ...
    source_symbol: Mapped[str] = mapped_column(String(64))
    period: Mapped[str] = mapped_column(String(16))              # YYYY-MM или YYYY-MM-DD
    url: Mapped[str] = mapped_column(Text)
    sha256: Mapped[str] = mapped_column(String(64))
    checksum_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    rows: Mapped[int] = mapped_column(Integer, default=0)
    raw_path: Mapped[str] = mapped_column(Text)                  # путь к raw Parquet
    downloaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("source", "dataset", "source_symbol", "period"),)


class GapRecord(Base):
    """Реестр гэпов 1m с причиной (3.2.3)."""

    __tablename__ = "gaps"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    hh_symbol: Mapped[str] = mapped_column(String(32), index=True)
    source: Mapped[str] = mapped_column(String(32))
    dataset: Mapped[str] = mapped_column(String(64), default="klines_1m")
    start_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))   # первая отсутствующая минута
    end_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))     # последняя отсутствующая минута
    minutes: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(32))  # listing|relisting|exchange_maintenance|source_outage|halted|unknown
    detail: Mapped[str] = mapped_column(Text, default="")
    filled: Mapped[bool] = mapped_column(Boolean, default=False)
    detected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    __table_args__ = (UniqueConstraint("hh_symbol", "source", "dataset", "start_ts"),)


class InstrumentStatus(Base):
    """Результат валидации инструмента (3.1.5)."""

    __tablename__ = "instrument_status"
    hh_symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ok: Mapped[bool] = mapped_column(Boolean, default=False)
    mapping_suspect: Mapped[bool] = mapped_column(Boolean, default=False)
    signals_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    issues: Mapped[list] = mapped_column(JSON, default=list)
    divergence_median_pct: Mapped[float | None] = mapped_column(Float, nullable=True)


# ---------------------------------------------------------------- бэктест и оптимизация (фазы 4–5)

class BacktestRun(Base):
    __tablename__ = "backtest_runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    kind: Mapped[str] = mapped_column(String(32))          # backtest | walkforward | holdout | sanity
    strategy: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    params_hash: Mapped[str] = mapped_column(String(64))
    dataset_hash: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str] = mapped_column(String(64))
    config: Mapped[dict] = mapped_column(JSON, default=dict)   # challenge-опции, горизонт, стресс
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    report_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    artifacts_dir: Mapped[str | None] = mapped_column(Text, nullable=True)  # Parquet сделок/событий


class Trial(Base):
    """Реестр испытаний: каждый проверенный вариант (раздел 7) — для DSR/PBO."""

    __tablename__ = "trials"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    study: Mapped[str] = mapped_column(String(128), index=True)   # имя исследования/оптимизации
    level: Mapped[str] = mapped_column(String(16))                # research | event
    strategy: Mapped[str] = mapped_column(String(64))
    group: Mapped[str] = mapped_column(String(32), default="")
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    params_hash: Mapped[str] = mapped_column(String(64))
    dataset_hash: Mapped[str] = mapped_column(String(64))
    code_version: Mapped[str] = mapped_column(String(64))
    window: Mapped[str] = mapped_column(String(64), default="")   # train/test окно walk-forward
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)
    block_perf: Mapped[list] = mapped_column(JSON, default=list)  # метрика по временным блокам (для PBO)


class CalibrationModel(Base):
    __tablename__ = "calibration_models"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    strategy: Mapped[str] = mapped_column(String(64))
    group: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    tf: Mapped[str] = mapped_column(String(8))
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    method: Mapped[str] = mapped_column(String(16), default="isotonic")
    model: Mapped[dict] = mapped_column(JSON, default=dict)       # пороги изотонической регрессии + корзины
    metrics: Mapped[dict] = mapped_column(JSON, default=dict)     # brier, n, reliability
    source_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)


# ---------------------------------------------------------------- live (фазы 6–7)

class LiveSignal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    bar_ts: Mapped[datetime] = mapped_column(DateTime(timezone=True))   # закрытие бара сигнала
    hh_symbol: Mapped[str] = mapped_column(String(32), index=True)
    tf: Mapped[str] = mapped_column(String(8))
    side: Mapped[str] = mapped_column(String(8))
    strategy: Mapped[str] = mapped_column(String(64))
    score: Mapped[float] = mapped_column(Float)
    prob: Mapped[dict | None] = mapped_column(JSON, nullable=True)      # {p, ci_low, ci_high, n, calibrated, low_data}
    ev_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    entry_type: Mapped[str] = mapped_column(String(16))
    entry: Mapped[float] = mapped_column(Float)
    sl: Mapped[float] = mapped_column(Float)
    tp1: Mapped[float] = mapped_column(Float)
    tp2: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp1_frac: Mapped[float] = mapped_column(Float, default=1.0)
    sl_trigger: Mapped[str] = mapped_column(String(8), default="last")
    plan: Mapped[dict] = mapped_column(JSON, default=dict)               # размер, риск по счетам
    reason: Mapped[dict] = mapped_column(JSON, default=dict)             # {pros, cons}
    components: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="new")      # new|active|triggered|expired|invalidated|tp|sl|timeout
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    hold_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    triggered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fill_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome_r: Mapped[float | None] = mapped_column(Float, nullable=True)
    taken: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    source: Mapped[str] = mapped_column(String(32), default="binance_um")
    tg_message_ids: Mapped[dict] = mapped_column(JSON, default=dict)     # {chat_id: message_id}
    events: Mapped[list["SignalEvent"]] = relationship(back_populates="signal", cascade="all, delete-orphan")
    __table_args__ = (Index("ix_signals_sym_side_status", "hh_symbol", "side", "status"),)


class SignalEvent(Base):
    __tablename__ = "signal_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[int] = mapped_column(ForeignKey("signals.id"), index=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    from_status: Mapped[str] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    signal: Mapped[LiveSignal] = relationship(back_populates="events")


class Account(Base):
    """Челлендж пользователя. Баланс/equity вводятся вручную (8.2)."""

    __tablename__ = "accounts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64))
    challenge_type: Mapped[str] = mapped_column(String(16))   # two_stage | one_stage
    market: Mapped[str] = mapped_column(String(16), default="crypto_tradfi")
    size: Mapped[float] = mapped_column(Float)
    stage: Mapped[str] = mapped_column(String(16), default="stage1")
    balance: Mapped[float] = mapped_column(Float)
    start_of_day_balance: Mapped[float] = mapped_column(Float)
    start_of_day_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    peak_equity: Mapped[float] = mapped_column(Float)
    trading_days: Mapped[list] = mapped_column(JSON, default=list)   # список prop_day_id
    status: Mapped[str] = mapped_column(String(16), default="active")
    dlp_available: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    positions: Mapped[list["ManualPosition"]] = relationship(back_populates="account", cascade="all, delete-orphan")


class ManualPosition(Base):
    __tablename__ = "positions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), index=True)
    hh_symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(8))
    qty: Mapped[float] = mapped_column(Float)
    entry_price: Mapped[float] = mapped_column(Float)
    sl: Mapped[float | None] = mapped_column(Float, nullable=True)
    tp: Mapped[float | None] = mapped_column(Float, nullable=True)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    signal_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    account: Mapped[Account] = relationship(back_populates="positions")


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Alert(Base):
    __tablename__ = "alerts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    kind: Mapped[str] = mapped_column(String(64))
    key: Mapped[str] = mapped_column(String(128), default="")
    message: Mapped[str] = mapped_column(Text)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
