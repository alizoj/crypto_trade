"""Помощники тестов стратегий (импортируются тестами и conftest)."""

from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import polars as pl

from quant.instruments import Instrument
from quant.schemas import TS

DAYS = 120
SYMBOL = "SYNUSDT"

# Пороги ниже боевых — чтобы на 120 днях синтетики было много сигналов в обе стороны.
TEST_PARAMS = {"long": {"threshold": 55}, "short": {"threshold": 55}}
TEST_PARAMS_FILTERS = {
    "long": {"threshold": 50}, "short": {"threshold": 50},
    "filters": {"adx_filter": True, "funding_oi_filter": True, "cvd_divergence_filter": True},
    "oi_hot_pct": 2.0, "funding_hot": 0.0004,
}
BASELINE_PARAMS = {"signals_per_day": 4.0, "seed": 7}
# Ослабленные пороги rsi_pin_ew — чтобы на синтетике было достаточно сигналов в обе стороны.
RSI_PIN_EW_PARAMS = {"rsi_long_max": 50, "rsi_short_min": 50, "rsi_lookback": 8, "pin_wick": 0.4, "pin_body": 0.5,
                     "pin_lookback": 4, "w1_min_atr": 1.0}


def ts_series(ms: np.ndarray) -> pl.Series:
    return pl.Series(np.asarray(ms, dtype=np.int64)).cast(pl.Datetime("ms")).dt.replace_time_zone("UTC").cast(TS)


def cut_meta(meta: dict, t_ms: int) -> dict:
    """Внешние ряды, известные строго до t_ms."""
    out = {}
    for k, v in meta.items():
        if isinstance(v, pl.DataFrame):
            tcol = next(c for c in ("ts", "create_time", "close_ts") if c in v.columns)
            out[k] = v.filter(pl.col(tcol).dt.epoch("ms") < t_ms)
        else:
            out[k] = v
    return out


def make_instrument(asset_class: str = "equity", underlying_type: str = "EQUITY", symbol: str = "TSLAUSDT"):
    ts = datetime(2025, 12, 11, tzinfo=timezone.utc)
    return Instrument(
        hh_symbol=symbol, base=symbol.replace("USDT", ""), asset_class=asset_class,  # type: ignore[arg-type]
        market="crypto_tradfi", underlying_type=underlying_type, max_per_lot_usd=50_000, max_total_usd=100_000,
        primary_source="binance_um", binance_um=symbol, binance_um_mult=1.0, bybit_linear=None,
        bybit_linear_mult=None, binance_spot=None, binance_spot_mult=None, binance_onboard=ts, bybit_launch=None,
        history_start=ts, notes="")


def make_strategy(name: str, filters: bool = False):
    from quant.strategies import get_strategy

    if name == "baseline_random":
        return get_strategy(name, BASELINE_PARAMS)
    if name == "rsi_pin_ew":
        return get_strategy(name, RSI_PIN_EW_PARAMS)
    return get_strategy(name, TEST_PARAMS_FILTERS if filters else TEST_PARAMS)
