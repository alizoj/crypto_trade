"""Сборка нормализованного слоя raw → norm для каждого hh_symbol (основной источник binance_um).

- множители (3.1.2): price / mult, qty * mult, notional без изменений; mark и цены funding — тоже;
- обрезка по history_start (с точностью до минуты): раньше в norm ничего не попадает (3.2.1);
- дедуп по ts с приоритетом version: архив 3 > REST 2 > WS 1 (dedup_1m);
- флаги: zero_vol, halted (серия ≥ halt_min_bars замороженных минут), synthetic=False — пропуски
  не заполняются (3.2.3). halted считается по полному ряду ДО обрезки, чтобы серия на границе
  history_start не укорачивалась;
- funding: ts = fundingTime, округлённый вниз до секунды; mark_price из REST, иначе open
  mark 1m той минуты.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import polars as pl

from quant.data import store
from quant.data.aggregate import dedup_1m
from quant.data.datasets import (REST_FUNDING, REST_KLINES, REST_MARK, SOURCE_BINANCE_UM, VERSION_ARCHIVE,
                                 VERSION_REST, period_of, raw_files)
from quant.data.flags import HALT_MIN_BARS, add_flags
from quant.instruments import Instrument, by_symbol
from quant.schemas import BARS_1M, conform

log = logging.getLogger(__name__)

NORM_DATASETS = ("bars_1m", "mark_1m", "funding", "metrics", "book_depth", "agg_trades")
_F = pl.Float64


# ---------------------------------------------------------------- нормализация (чистые функции)

def _f64(col: str) -> pl.Expr:
    """Decimal → Float64 с корректным округлением.

    Прямой cast(Decimal(38,18) → Float64) в Polars делит int128 на 1e18 и даёт 10499.999999999998
    вместо 10500 — поэтому через строку (ближайший double к десятичному значению).
    """
    return pl.col(col).cast(pl.Utf8).cast(_F)


def _price(col: str, mult: float) -> pl.Expr:
    e = _f64(col)
    return (e / mult if mult != 1 else e).alias(col)


def _qty(col: str, mult: float, alias: str | None = None) -> pl.Expr:
    e = _f64(col)
    return (e * mult if mult != 1 else e).alias(alias or col)


def _ts(col: str) -> pl.Expr:
    return pl.col(col).dt.cast_time_unit("ms").dt.convert_time_zone("UTC").alias("ts")


def normalize_klines(raw: pl.DataFrame | pl.LazyFrame, mult: float) -> pl.DataFrame | pl.LazyFrame:
    """raw klines (Decimal, как пришло) → нормализованные float-бары за 1 единицу базового актива."""
    return raw.select(
        _ts("open_time"),
        _price("open", mult), _price("high", mult), _price("low", mult), _price("close", mult),
        _qty("volume", mult),
        _f64("quote_volume"),
        pl.col("count").cast(pl.Int64).alias("trades"),
        _qty("taker_buy_volume", mult),
        _f64("taker_buy_quote_volume"),
    )


def normalize_mark(raw: pl.DataFrame | pl.LazyFrame, mult: float) -> pl.DataFrame | pl.LazyFrame:
    return raw.select(_ts("open_time"), _price("open", mult), _price("high", mult),
                      _price("low", mult), _price("close", mult))


def funding_ts(col: str) -> pl.Expr:
    """fundingTime/calc_time → ts, округлённый ВНИЗ до секунды (…800001 → …800000)."""
    return pl.col(col).dt.truncate("1s").dt.cast_time_unit("ms").dt.convert_time_zone("UTC").alias("ts")


# ---------------------------------------------------------------- выбор raw-файлов

def select_raw_files(dataset: str, source_symbol: str, *, data_dir: Path | None = None) -> list[Path]:
    """Месячный файл перекрывает дневные того же месяца (одни и те же данные)."""
    files = raw_files(dataset, source_symbol, data_dir=data_dir)
    monthly = {period_of(p): p for p in files if re.fullmatch(r"\d{4}-\d{2}", period_of(p))}
    out = list(monthly.values())
    for p in files:
        per = period_of(p)
        if len(per) == 10 and per[:7] not in monthly:
            out.append(p)
    return sorted(out, key=lambda p: period_of(p))


def _scan(files: list[Path]) -> pl.LazyFrame | None:
    return pl.scan_parquet(files) if files else None


def _cut(df: pl.DataFrame, inst: Instrument) -> pl.DataFrame:
    return df.filter(pl.col("ts") >= inst.history_start)


def _write_years(df: pl.DataFrame, d: Path) -> dict:
    """Разложить по годам; перезаписать только изменившиеся, удалить лишние."""
    years = sorted(df.get_column("ts").dt.year().unique().to_list()) if df.height else []
    written = 0
    for y in years:
        part = df.filter(pl.col("ts").dt.year() == y)
        path = d / f"{y}.parquet"
        dg = store.frame_digest(part)
        if path.exists() and store.read_meta(path).get(store.META_SHA) == dg:
            continue
        store.write_norm(part, path)
        written += 1
    if d.exists():
        for p in d.glob("*.parquet"):
            if re.fullmatch(r"\d{4}\.parquet", p.name) and int(p.stem) not in years:
                p.unlink()
    return {"years": years, "files_written": written}


def _write_one(df: pl.DataFrame, path: Path) -> bool:
    if df.is_empty():
        if path.exists():
            path.unlink()
        return False
    dg = store.frame_digest(df)
    if path.exists() and store.read_meta(path).get(store.META_SHA) == dg:
        return False
    store.write_norm(df, path)
    return True


# ---------------------------------------------------------------- датасеты

def _check_primary(inst: Instrument) -> str:
    if inst.primary_source != SOURCE_BINANCE_UM:
        raise NotImplementedError(f"{inst.hh_symbol}: primary_source={inst.primary_source} пока не поддержан")
    return inst.binance_um


def bars_frame(inst: Instrument, *, halt_min_bars: int = HALT_MIN_BARS, data_dir: Path | None = None,
               cut: bool = True) -> pl.DataFrame:
    """Склеенный нормализованный ряд 1m (архив v3 + REST v2) с флагами; cut — обрезка по history_start."""
    sym = _check_primary(inst)
    mult = inst.multiplier(SOURCE_BINANCE_UM)
    parts = []
    for files, version in ((select_raw_files("klines_1m", sym, data_dir=data_dir), VERSION_ARCHIVE),
                           (raw_files(REST_KLINES, sym, data_dir=data_dir), VERSION_REST)):
        lf = _scan(files)
        if lf is not None:
            parts.append(normalize_klines(lf, mult).with_columns(pl.lit(version, dtype=pl.Int8).alias("version")))
    if not parts:
        return pl.DataFrame(schema=BARS_1M)
    df = dedup_1m(pl.concat(parts, how="vertical").collect())
    df = add_flags(df, halt_min_bars).with_columns(pl.lit(SOURCE_BINANCE_UM).alias("source"))
    df = conform(df, BARS_1M, extra=False)
    return _cut(df, inst) if cut else df


def build_bars(inst: Instrument, *, halt_min_bars: int = HALT_MIN_BARS, data_dir: Path | None = None) -> dict:
    df = bars_frame(inst, halt_min_bars=halt_min_bars, data_dir=data_dir)
    res = _write_years(df, store.bars_dir(inst.hh_symbol, data_dir))
    res.update(rows=df.height, halted=int(df.get_column("halted").sum()) if df.height else 0,
               rest_rows=int((df.get_column("version") == VERSION_REST).sum()) if df.height else 0)
    return res


def build_mark(inst: Instrument, *, data_dir: Path | None = None) -> dict:
    sym = _check_primary(inst)
    mult = inst.multiplier(SOURCE_BINANCE_UM)
    parts = []
    for files, version in ((select_raw_files("markPriceKlines_1m", sym, data_dir=data_dir), VERSION_ARCHIVE),
                           (raw_files(REST_MARK, sym, data_dir=data_dir), VERSION_REST)):
        lf = _scan(files)
        if lf is not None:
            parts.append(normalize_mark(lf, mult).with_columns(pl.lit(version, dtype=pl.Int8).alias("version")))
    if not parts:
        return {"rows": 0}
    df = _cut(dedup_1m(pl.concat(parts, how="vertical").collect()), inst)
    df = conform(df, store.MARK_STORE, extra=False)
    res = _write_years(df, store.mark_dir(inst.hh_symbol, data_dir))
    res["rows"] = df.height
    return res


def funding_frame(inst: Instrument, *, data_dir: Path | None = None) -> pl.DataFrame:
    sym = _check_primary(inst)
    mult = inst.multiplier(SOURCE_BINANCE_UM)
    a = _scan(select_raw_files("fundingRate", sym, data_dir=data_dir))
    r = _scan(raw_files(REST_FUNDING, sym, data_dir=data_dir))
    if a is None and r is None:
        return pl.DataFrame(schema=store.FUNDING_STORE)
    if a is not None:
        a = (a.select(funding_ts("calc_time"), _f64("last_funding_rate").alias("rate_a"),
                      pl.col("funding_interval_hours").cast(pl.Int16).alias("interval_hours"))
             .unique(subset=["ts"], keep="last").collect())
    if r is not None:
        r = (r.select(funding_ts("fundingTime"), _f64("fundingRate").alias("rate_r"),
                      _price("markPrice", mult).alias("mark_r"))
             .unique(subset=["ts"], keep="last").collect())
    if a is None:
        j = r.with_columns(pl.lit(None, dtype=_F).alias("rate_a"), pl.lit(None, dtype=pl.Int16).alias("interval_hours"))
    elif r is None:
        j = a.with_columns(pl.lit(None, dtype=_F).alias("rate_r"), pl.lit(None, dtype=_F).alias("mark_r"))
    else:
        j = a.join(r, on="ts", how="full", coalesce=True)
    j = j.sort("ts")
    # mark 1m той минуты — фоллбэк для mark_price (open бара, в котором произошло начисление)
    mk = store.load_mark(inst.hh_symbol, data_dir=data_dir).select(
        pl.col("ts").alias("ts_min"), pl.col("open").alias("m_open"))
    j = (j.with_columns(pl.col("ts").dt.truncate("1m").alias("ts_min"))
         .join(mk, on="ts_min", how="left"))
    gap_h = (pl.col("ts").diff().dt.total_seconds() / 3600).round(0).cast(pl.Int16)
    out = j.select(
        "ts",
        pl.coalesce("rate_a", "rate_r").alias("rate"),
        pl.coalesce("mark_r", "m_open").alias("mark_price"),
        pl.lit(SOURCE_BINANCE_UM).alias("source"),
        pl.coalesce("interval_hours", gap_h).alias("interval_hours"),
    ).filter(pl.col("rate").is_not_null())
    return conform(_cut(out, inst), store.FUNDING_STORE, extra=False).sort("ts")


def build_funding(inst: Instrument, *, data_dir: Path | None = None) -> dict:
    df = funding_frame(inst, data_dir=data_dir)
    _write_one(df, store.funding_path(inst.hh_symbol, data_dir))
    return {"rows": df.height, "mark_null": int(df.get_column("mark_price").null_count()) if df.height else 0}


def build_metrics(inst: Instrument, *, data_dir: Path | None = None) -> dict:
    sym = _check_primary(inst)
    mult = inst.multiplier(SOURCE_BINANCE_UM)
    lf = _scan(select_raw_files("metrics", sym, data_dir=data_dir))
    if lf is None:
        return {"rows": 0}
    df = (lf.select(
        _ts("create_time"),
        _qty("sum_open_interest", mult),
        *[_f64(c) for c in ("sum_open_interest_value", "count_toptrader_long_short_ratio",
                                        "sum_toptrader_long_short_ratio", "count_long_short_ratio",
                                        "sum_taker_long_short_vol_ratio")],
    ).unique(subset=["ts"], keep="last").sort("ts").collect())
    df = conform(_cut(df, inst), store.METRICS_COLS, extra=False)
    _write_one(df, store.metrics_path(inst.hh_symbol, data_dir))
    return {"rows": df.height}


def build_book_depth(inst: Instrument, *, data_dir: Path | None = None) -> dict:
    sym = _check_primary(inst)
    mult = inst.multiplier(SOURCE_BINANCE_UM)
    lf = _scan(select_raw_files("bookDepth", sym, data_dir=data_dir))
    if lf is None:
        return {"rows": 0}
    df = (lf.select(_ts("timestamp"), _f64("percentage"), _qty("depth", mult),
                    _f64("notional"))
          .unique(subset=["ts", "percentage"], keep="last").sort(["ts", "percentage"]).collect())
    df = conform(_cut(df, inst), store.BOOK_DEPTH_COLS, extra=False)
    res = _write_years(df, store.book_depth_dir(inst.hh_symbol, data_dir))
    res["rows"] = df.height
    return res


def normalize_agg_trades(raw: pl.LazyFrame | pl.DataFrame, mult: float):
    return raw.select(
        _ts("transact_time"),
        pl.col("agg_trade_id").alias("agg_id"),
        _price("price", mult),
        _qty("quantity", mult, "qty"),
        pl.col("first_trade_id").alias("first_id"),
        pl.col("last_trade_id").alias("last_id"),
        pl.col("is_buyer_maker"),
    )


def build_agg_trades(inst: Instrument, *, data_dir: Path | None = None, force: bool = False) -> dict:
    """По месяцам, потоково (sink_parquet): месяц пересобирается, только если raw новее norm."""
    sym = _check_primary(inst)
    mult = inst.multiplier(SOURCE_BINANCE_UM)
    by_month: dict[str, list[Path]] = {}
    for p in select_raw_files("aggTrades", sym, data_dir=data_dir):
        by_month.setdefault(period_of(p)[:7], []).append(p)
    d = store.agg_trades_dir(inst.hh_symbol, data_dir)
    d.mkdir(parents=True, exist_ok=True)
    built = 0
    for month, files in sorted(by_month.items()):
        out = d / f"{month}.parquet"
        if not force and out.exists() and out.stat().st_mtime >= max(f.stat().st_mtime for f in files):
            continue
        lf = normalize_agg_trades(pl.scan_parquet(files), mult).filter(pl.col("ts") >= inst.history_start)
        tmp = out.with_suffix(".parquet.tmp")
        lf.sink_parquet(tmp, compression="zstd")
        store._replace(tmp, out)
        built += 1
    return {"months": sorted(by_month), "files_written": built}


BUILDERS = {
    "bars_1m": build_bars, "mark_1m": build_mark, "funding": build_funding,
    "metrics": build_metrics, "book_depth": build_book_depth, "agg_trades": build_agg_trades,
}


def build_symbol(hh_symbol: str, datasets: list[str] | None = None, *, halt_min_bars: int = HALT_MIN_BARS,
                 data_dir: Path | None = None) -> dict:
    """Собрать norm по символу. Порядок важен: mark раньше funding (фоллбэк mark_price)."""
    inst = by_symbol()[hh_symbol]
    todo = [d for d in NORM_DATASETS if datasets is None or d in datasets]
    res: dict[str, dict] = {}
    for name in todo:
        fn = BUILDERS[name]
        kw = {"halt_min_bars": halt_min_bars} if name == "bars_1m" else {}
        res[name] = fn(inst, data_dir=data_dir, **kw)
    log.info("build %s: %s", hh_symbol,
             ", ".join(f"{k}={v.get('rows', v.get('months', ''))}" for k, v in res.items()
                       if not isinstance(v.get("months"), list)) or "-")
    return res


def build(symbols: list[str], datasets: list[str] | None = None, *, halt_min_bars: int = HALT_MIN_BARS,
          data_dir: Path | None = None) -> dict[str, dict]:
    out = {}
    for hh in symbols:
        try:
            out[hh] = build_symbol(hh, datasets, halt_min_bars=halt_min_bars, data_dir=data_dir)
        except Exception as e:  # noqa: BLE001 — один символ не должен ронять сборку остальных
            log.exception("build %s: ошибка", hh)
            out[hh] = {"error": str(e)}
    return out
