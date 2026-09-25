"""Конфиги: правила челленджа (config/challenge.yaml), настройки окружения, конфиги стратегий.

Никаких цифр правил в коде — только схемы и загрузка.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


def _hhmm_to_minutes(value: str | None) -> int | None:
    if value is None:
        return None
    hh, mm = value.split(":")
    return int(hh) * 60 + int(mm)


class Fees(BaseModel):
    maker: float
    taker: float
    min_fee_usd: float = 0.0


class MarketRules(BaseModel):
    day_boundary_utc: str
    reset_window_minutes: int = 10
    fees: Fees
    max_leverage: float
    leverage_base: Literal["equity", "balance"]
    block_new_utc: str | None = None
    auto_close_utc: str | None = None
    max_shares_per_ticker: int | None = None

    @property
    def day_boundary_min(self) -> int:
        return _hhmm_to_minutes(self.day_boundary_utc)  # type: ignore[return-value]

    @property
    def block_new_min(self) -> int | None:
        return _hhmm_to_minutes(self.block_new_utc)

    @property
    def auto_close_min(self) -> int | None:
        return _hhmm_to_minutes(self.auto_close_utc)


class StageSpec(BaseModel):
    name: str
    target_pct: float | None = None
    daily_loss_pct: float
    max_loss_pct: float
    min_trading_days: int = 0


class TraderSpec(BaseModel):
    daily_loss_pct: float
    max_loss_pct: float


class ChallengeSpec(BaseModel):
    sizes: list[int]
    stages: list[StageSpec]
    trader: TraderSpec


class LiquidationOptions(BaseModel):
    enabled: bool = True
    maintenance_margin_rate: float = 0.005


class ChallengeOptions(BaseModel):
    total_floor_mode: Literal["static", "trailing"] = "static"
    trailing_reference: Literal["equity", "balance"] = "equity"
    target_metric: Literal["balance", "equity"] = "balance"
    reset_window_mode: Literal["new_day", "old_day", "both"] = "both"
    stage_start_balance: Literal["account_size"] = "account_size"
    equity_price_source: Literal["mark", "last"] = "mark"
    daily_loss_protection: bool = False
    daily_loss_reset: bool = False
    liquidation: LiquidationOptions = Field(default_factory=LiquidationOptions)


class PayoutSpec(BaseModel):
    profit_share: float
    profit_share_with_split: float
    period_days: int
    max_usd_per_period: float
    min_usd: float
    require_flat: bool = True


class ChallengeConfig(BaseModel):
    markets: dict[str, MarketRules]
    challenges: dict[str, ChallengeSpec]
    options: ChallengeOptions = Field(default_factory=ChallengeOptions)
    payouts: PayoutSpec
    challenge_fee_usd: dict[str, dict[int, float]] = Field(default_factory=dict)

    def market(self, name: str) -> MarketRules:
        return self.markets[name]

    def stage(self, challenge_type: str, stage_name: str) -> StageSpec:
        spec = self.challenges[challenge_type]
        if stage_name == "trader":
            t = spec.trader
            return StageSpec(name="trader", target_pct=None, daily_loss_pct=t.daily_loss_pct,
                             max_loss_pct=t.max_loss_pct, min_trading_days=0)
        for s in spec.stages:
            if s.name == stage_name:
                return s
        raise KeyError(f"{challenge_type}: нет стадии {stage_name}")

    def stage_names(self, challenge_type: str) -> list[str]:
        return [s.name for s in self.challenges[challenge_type].stages]

    def with_options(self, **overrides: Any) -> "ChallengeConfig":
        opts = ChallengeOptions.model_validate({**self.options.model_dump(), **overrides})
        return self.model_copy(update={"options": opts})


def load_challenge_config(path: str | Path | None = None) -> ChallengeConfig:
    p = Path(path) if path else CONFIG_DIR / "challenge.yaml"
    with open(p, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return ChallengeConfig.model_validate(raw)


class Settings(BaseSettings):
    """Настройки окружения. Всё берётся из переменных QUANT_* или .env."""

    model_config = SettingsConfigDict(env_prefix="QUANT_", env_file=".env", extra="ignore")

    data_dir: Path = ROOT / "data"
    db_url: str = ""                      # пусто → SQLite в data_dir/quant.db
    redis_url: str = ""                   # пусто → шина в памяти (локальный режим)
    clickhouse_url: str = ""              # пусто → live-бары пишутся в Parquet
    instruments_csv: Path = CONFIG_DIR / "instruments.csv"
    challenge_yaml: Path = CONFIG_DIR / "challenge.yaml"
    strategies_dir: Path = CONFIG_DIR / "strategies"
    reports_dir: Path = ROOT / "reports_out"

    telegram_token: str = ""
    telegram_chat_ids: str = ""           # через запятую — whitelist
    telegram_channel_id: str = ""         # приватный канал (опционально)
    web_base_url: str = "http://localhost:8000"
    quiet_hours_utc: str = ""             # "22:00-06:00"

    session_secret: str = "change-me"
    cookie_secure: bool = False
    alpaca_key: str = ""                  # фаза 8: US Stocks (Alpaca Market Data)
    alpaca_secret: str = ""
    anthropic_api_key: str = ""           # опционально, краткое резюме сетапа (8.3)
    llm_summary_enabled: bool = False

    network_tests: bool = False

    @field_validator("data_dir", "reports_dir", mode="after")
    @classmethod
    def _abs(cls, v: Path) -> Path:
        return v if v.is_absolute() else (ROOT / v)

    @property
    def effective_db_url(self) -> str:
        if self.db_url:
            return self.db_url
        self.data_dir.mkdir(parents=True, exist_ok=True)
        return f"sqlite:///{(self.data_dir / 'quant.db').as_posix()}"

    @property
    def chat_ids(self) -> set[int]:
        return {int(x) for x in self.telegram_chat_ids.split(",") if x.strip()}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def load_strategy_config(name: str, directory: Path | None = None) -> dict[str, Any]:
    d = directory or get_settings().strategies_dir
    with open(Path(d) / f"{name}.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes"}
