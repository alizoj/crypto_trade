"""signals_from → схема SIGNALS; halted / synthetic / TradFi вне сессии — без сигналов;
реестр стратегий и конфиги; explain."""

from __future__ import annotations

import numpy as np
import polars as pl
import pytest
import yaml
from strat_helpers import BASELINE_PARAMS, FEATURE_RULES_PARAMS, RSI_PIN_EW_PARAMS, SYMBOL, TEST_PARAMS, make_instrument, make_strategy

from quant.config import CONFIG_DIR
from quant.schemas import SIGNALS
from quant.sessions import open_mask
from quant.strategies import STRATEGIES, get_strategy, load_params, set_dotted, with_overrides
from quant.strategies.confluence_v1 import FACTORS

NAMES = ["confluence_v1", "baseline_random", "rsi_pin_ew", "feature_rules"]
SCREENER = ("rsi14", "adx", "atr_pct", "vwap_dist_pct", "htf_trend", "cvd_trend")


@pytest.mark.parametrize("name", NAMES)
def test_frame_contract_and_signals_schema(bars_1h, bars_4h, name):
    s = make_strategy(name)
    fr = s.compute(bars_1h, hh_symbol=SYMBOL, tf="1h", htf=bars_4h)
    assert fr.height == bars_1h.height                                   # строка на каждый бар
    for col in ("ts", "close_ts", "score_long", "score_short", "signal_long", "signal_short", *SCREENER,
                *(f"{k}_{sd}" for k in ("entry", "sl", "tp1", "tp2") for sd in ("long", "short"))):
        assert col in fr.columns, col
    assert (fr["close_ts"] - fr["ts"]).dt.total_minutes().unique().to_list() == [60]
    assert fr["htf_trend"].drop_nulls().is_in([-1, 0, 1]).all()
    assert fr["cvd_trend"].drop_nulls().is_in([-1, 0, 1]).all()
    sc = fr["score_long"].drop_nulls()
    assert sc.min() >= 0 and sc.max() <= 100
    sig = s.signals_from(fr, hh_symbol=SYMBOL, tf="1h")
    assert list(sig.columns[:len(SIGNALS)]) == list(SIGNALS)
    for k, dt in SIGNALS.items():
        assert sig.schema[k] == dt, k
    assert sig.height == int(fr["signal_long"].sum() + fr["signal_short"].sum()) > 0
    assert sig["side"].null_count() == 0 and sig["ts"].is_sorted()
    longs, shorts = sig.filter(pl.col("side") == 1), sig.filter(pl.col("side") == -1)
    assert (longs["sl"] < longs["entry_price"]).all() and (longs["tp1"] > longs["entry_price"]).all()
    assert (shorts["sl"] > shorts["entry_price"]).all() and (shorts["tp1"] < shorts["entry_price"]).all()
    if name == "confluence_v1":
        assert (longs["tp2"] > longs["tp1"]).all() and (sig["tp1_frac"] == 0.5).all()
        assert {f"c_{k}" for k in FACTORS} <= set(sig.columns)
        # шорт — только при медвежьем HTF
        sh = fr.filter(pl.col("signal_short"))
        assert (sh["htf_trend"] == -1).all()
    elif name == "rsi_pin_ew":
        assert (longs["tp2"] > longs["tp1"]).all() and (shorts["tp2"] < shorts["tp1"]).all()
        assert {"c_trend", "c_rsi", "c_pinbar", "c_elliott"} <= set(sig.columns)
        assert (fr.filter(pl.col("signal_long"))["htf_trend"] == 1).all()
        assert (fr.filter(pl.col("signal_short"))["htf_trend"] == -1).all()
    else:
        assert sig["tp2"].null_count() == sig.height and (sig["tp1_frac"] == 1.0).all()


@pytest.mark.parametrize("name", NAMES)
def test_no_phantom_signal_without_signals(bars_1h, bars_4h, name):
    """signals_from без сигналов — 0 строк (обход бага conform(pl.DataFrame()) → 1 строка null)."""
    s = make_strategy(name)
    fr = s.compute(bars_1h, hh_symbol=SYMBOL, tf="1h", htf=bars_4h)
    quiet = fr.filter(~pl.col("signal_long") & ~pl.col("signal_short")).tail(1)
    assert s.signals_from(quiet, hh_symbol=SYMBOL, tf="1h").height == 0


@pytest.mark.parametrize("name", NAMES)
def test_halted_bars(bars_1h, bars_4h, name):
    s = make_strategy(name)
    base = s.compute(bars_1h, hh_symbol=SYMBOL, tf="1h", htf=bars_4h)
    sig_idx = np.flatnonzero((base["signal_long"] | base["signal_short"]).to_numpy())
    halted = np.zeros(bars_1h.height, dtype=bool)
    halted[sig_idx[:5]] = True                                           # бары, где были сигналы
    halted[1500:1530] = True
    b = bars_1h.with_columns(pl.Series("halted", halted))
    fr = s.compute(b, hh_symbol=SYMBOL, tf="1h", htf=bars_4h)
    assert fr.height == bars_1h.height
    h = fr.filter(pl.col("halted"))
    assert h.height == halted.sum()
    assert not h["signal_long"].any() and not h["signal_short"].any()
    assert h["score_long"].null_count() == h.height and h["rsi14"].null_count() == h.height
    # индикаторы halted-баров не получают: остальные строки = расчёт без этих баров вовсе
    ref = s.compute(bars_1h.filter(~pl.Series(halted)), hh_symbol=SYMBOL, tf="1h", htf=bars_4h)
    live = fr.filter(~pl.col("halted"))
    for col in ("score_long", "score_short", "rsi14", "adx", "signal_long", "signal_short"):
        assert live[col].equals(ref[col]), col


@pytest.mark.parametrize("name", NAMES)
def test_synthetic_bars_blocked(bars_1h, bars_4h, name):
    s = make_strategy(name)
    base = s.compute(bars_1h, hh_symbol=SYMBOL, tf="1h", htf=bars_4h)
    sig_idx = np.flatnonzero((base["signal_long"] | base["signal_short"]).to_numpy())[:5]
    syn = np.zeros(bars_1h.height, dtype=bool)
    syn[sig_idx] = True
    fr = s.compute(bars_1h.with_columns(pl.Series("has_synthetic", syn)), hh_symbol=SYMBOL, tf="1h", htf=bars_4h)
    assert not (fr["signal_long"] | fr["signal_short"]).to_numpy()[syn].any()


@pytest.mark.parametrize("name", NAMES)
def test_tradfi_off_session(bars_1h, bars_4h, name):
    inst = make_instrument()
    s = make_strategy(name)
    fr = s.compute(bars_1h, hh_symbol="TSLAUSDT", tf="1h", htf=bars_4h, meta={"instrument": inst})
    close_ms = fr["close_ts"].dt.epoch("ms").to_numpy()
    is_open = open_mask("EQUITY", close_ms)
    sig = (fr["signal_long"] | fr["signal_short"]).to_numpy()
    assert sig.sum() > 0 and not sig[~is_open].any()
    assert (fr["session_open"].to_numpy() == is_open).all() if "session_open" in fr.columns else True
    # флаг tradfi_off_session_signals: true — сигналы и вне сессии
    base_params = {"baseline_random": BASELINE_PARAMS, "rsi_pin_ew": RSI_PIN_EW_PARAMS,
                   "feature_rules": FEATURE_RULES_PARAMS}.get(name, TEST_PARAMS)
    params = {**base_params,
              "filters": {"tradfi_off_session_signals": True}}
    s_on = get_strategy(name, params)
    fr_on = s_on.compute(bars_1h, hh_symbol="TSLAUSDT", tf="1h", htf=bars_4h, meta={"instrument": inst})
    assert (fr_on["signal_long"] | fr_on["signal_short"]).to_numpy()[~is_open].any()
    # крипта (COIN) — сессия не ограничивает
    crypto = make_instrument("crypto", "COIN", "BTCUSDT")
    fr_c = s.compute(bars_1h, hh_symbol="TSLAUSDT", tf="1h", htf=bars_4h, meta={"instrument": crypto})
    assert (fr_c["signal_long"] | fr_c["signal_short"]).to_numpy()[~is_open].any()


def test_premarket_never_open(bars_1h, bars_4h):
    inst = make_instrument("equity", "PREMARKET", "OPENAIUSDT")
    fr = make_strategy("baseline_random").compute(bars_1h, hh_symbol="OPENAIUSDT", tf="1h", htf=bars_4h,
                                                  meta={"instrument": inst})
    assert not (fr["signal_long"] | fr["signal_short"]).any()


def test_baseline_rate_and_balance(bars_1h):
    s = get_strategy("baseline_random", {"signals_per_day": 2.0, "seed": 1})
    fr = s.compute(bars_1h, hh_symbol=SYMBOL, tf="1h")
    n_long, n_short = int(fr["signal_long"].sum()), int(fr["signal_short"].sum())
    days = bars_1h.height / 24
    assert 1.6 < (n_long + n_short) / days < 2.4
    assert 0.35 < n_long / (n_long + n_short) < 0.65
    assert fr.filter(pl.col("signal_long"))["score_long"].unique().to_list() == [50.0]
    other = get_strategy("baseline_random", {"signals_per_day": 2.0, "seed": 2}).compute(bars_1h, hh_symbol=SYMBOL,
                                                                                          tf="1h")
    assert not other["signal_long"].equals(fr["signal_long"])
    sl = fr.filter(pl.col("signal_long"))
    risk, reward = sl["entry_long"] - sl["sl_long"], sl["tp1_long"] - sl["entry_long"]
    np.testing.assert_allclose(reward.to_numpy(), risk.to_numpy(), rtol=1e-12)     # RR = 1


def test_baseline_short_share(bars_1h):
    base = get_strategy("baseline_random", {"signals_per_day": 2.0, "seed": 1}).compute(bars_1h, hh_symbol=SYMBOL,
                                                                                          tf="1h")
    skew = get_strategy("baseline_random", {"signals_per_day": 2.0, "seed": 1, "short_share": 0.82}).compute(
        bars_1h, hh_symbol=SYMBOL, tf="1h")
    fire = lambda fr: (fr["signal_long"] | fr["signal_short"]).to_numpy()  # noqa: E731
    np.testing.assert_array_equal(fire(skew), fire(base))                # доля шортов не меняет моменты входа
    n_long, n_short = int(skew["signal_long"].sum()), int(skew["signal_short"].sum())
    assert 0.72 < n_short / (n_long + n_short) < 0.92
    only_short = get_strategy("baseline_random", {"signals_per_day": 2.0, "seed": 1, "short_share": 1.0}).compute(
        bars_1h, hh_symbol=SYMBOL, tf="1h")
    assert not only_short["signal_long"].any() and only_short["signal_short"].sum() == fire(base).sum()


def test_registry_and_params(tmp_path):
    assert set(STRATEGIES) == {"confluence_v1", "baseline_random", "rsi_pin_ew", "feature_rules"}
    for name in STRATEGIES:
        s = get_strategy(name)                                           # из config/strategies/<name>.yaml
        assert s.name == name
        cfg = yaml.safe_load(open(CONFIG_DIR / "strategies" / f"{name}.yaml", encoding="utf-8"))
        assert {"risk", "execution", "filters", "params", "groups", "search_space"} <= set(cfg)
        assert 1 <= len(cfg["search_space"]) <= 6
        p = load_params(name)
        for path in cfg["search_space"]:                                 # пути search_space существуют
            node = p
            for k in path.split("."):
                assert k in node, f"{name}: {path}"
                node = node[k]
        assert p["filters"] == cfg["filters"] and p["tf"] == cfg["tf"] and p["htf"] == cfg["htf"]
    with pytest.raises(KeyError):
        get_strategy("nope")

    cfg = {"name": "x", "tf": "15m", "htf": "1h", "params": {"a": 1, "long": {"threshold": 60, "sl_atr": 1.5}},
           "groups": {"tradfi": {"long": {"threshold": 75}, "filters": {"adx_filter": True}}},
           "filters": {"adx_filter": False, "tradfi_off_session_signals": False}}
    (tmp_path / "x.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")
    p = load_params("x", "tradfi", directory=tmp_path)
    assert p["long"] == {"threshold": 75, "sl_atr": 1.5} and p["a"] == 1
    assert p["filters"] == {"adx_filter": True, "tradfi_off_session_signals": False}
    assert p["tf"] == "15m" and p["htf"] == "1h"
    assert load_params("x", directory=tmp_path)["long"]["threshold"] == 60
    q = set_dotted(p, "long.threshold", 80)
    assert q["long"]["threshold"] == 80 and p["long"]["threshold"] == 75
    assert with_overrides(p, {"long.sl_atr": 2.0, "short.threshold": 70})["short"] == {"threshold": 70}


def test_explain_factors(bars_1h, bars_4h, meta):
    s = make_strategy("confluence_v1")
    fr = s.compute(bars_1h, hh_symbol=SYMBOL, tf="1h", htf=bars_4h, meta=meta)
    wsum = sum(s.params["weights"].values())
    rows = fr.filter(pl.col("signal_long") | pl.col("signal_short")).head(20).to_dicts()
    rows += fr.filter(pl.col("score_long").is_not_null()).sample(20, seed=1).to_dicts()
    for row in rows:
        for side in ("long", "short"):
            fs = s.explain(row, side)
            names = [f.name for f in fs]
            assert names[:len(FACTORS)] == list(FACTORS)
            for f in fs:
                assert f.label and f.verdict in ("pro", "con", "neutral")
                assert 0 <= f.points <= f.max_points + 1e-9
            total = sum(f.points for f in fs if f.max_points > 0)
            assert total / wsum * 100 == pytest.approx(row[f"score_{side}"], abs=1e-9)
            assert {"adx", "funding", "oi", "btc_trend"} <= set(names)
    sig_row = fr.filter(pl.col("signal_long")).row(0, named=True)
    labels = [f.label for f in s.explain(sig_row, "long") if f.verdict == "pro"]
    assert labels and all(isinstance(x, str) for x in labels)
    htf_f = s.explain(sig_row, "long")[0]
    assert "EMA200 4H" in htf_f.label
