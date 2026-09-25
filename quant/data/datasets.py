"""Реестр датасетов архива data.binance.vision и раскладка файлов на диске.

Raw (как пришло, типизировано, без нормализации):
    data_dir/raw/<source>/<dataset>/<source_symbol>/<source_symbol>-<dataset>-<period>.parquet
Norm (нормализовано, обрезано по history_start, дедуп) — см. quant.data.store.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from quant.config import get_settings

SOURCE_BINANCE_UM = "binance_um"

# Версии строк (3.2.5): архив > REST > WS.
VERSION_ARCHIVE = 3
VERSION_REST = 2
VERSION_WS = 1


@dataclass(frozen=True)
class DatasetSpec:
    name: str            # внутреннее имя (манифест, raw-каталог): klines_1m, fundingRate, ...
    path: str            # путь в архиве после futures/um/<freq>/, {sym} — тикер источника
    file_tag: str        # средняя часть имени файла: <SYM>-<file_tag>-<period>.zip
    kind: str            # парсер: klines | funding | metrics | book_depth | agg_trades
    monthly: bool
    daily: bool

    def prefix(self, freq: str, source_symbol: str) -> str:
        return f"data/futures/um/{freq}/{self.path.format(sym=source_symbol)}/"

    def file_re(self, source_symbol: str, freq: str) -> re.Pattern[str]:
        """Строгий шаблон имени: посторонние файлы (part-00000-….zip) не проходят."""
        period = r"\d{4}-\d{2}" if freq == "monthly" else r"\d{4}-\d{2}-\d{2}"
        return re.compile(rf"^{re.escape(source_symbol)}-{re.escape(self.file_tag)}-({period})\.zip$")


DATASETS: dict[str, DatasetSpec] = {
    "klines_1m": DatasetSpec("klines_1m", "klines/{sym}/1m", "1m", "klines", True, True),
    "markPriceKlines_1m": DatasetSpec("markPriceKlines_1m", "markPriceKlines/{sym}/1m", "1m", "klines", True, True),
    "indexPriceKlines_1m": DatasetSpec("indexPriceKlines_1m", "indexPriceKlines/{sym}/1m", "1m", "klines", True, True),
    "fundingRate": DatasetSpec("fundingRate", "fundingRate/{sym}", "fundingRate", "funding", True, False),
    "metrics": DatasetSpec("metrics", "metrics/{sym}", "metrics", "metrics", False, True),
    "bookDepth": DatasetSpec("bookDepth", "bookDepth/{sym}", "bookDepth", "book_depth", False, True),
    "aggTrades": DatasetSpec("aggTrades", "aggTrades/{sym}", "aggTrades", "agg_trades", True, True),
}

# Имена в CLI → внутренние.
ALIASES = {
    "klines": "klines_1m", "klines_1m": "klines_1m",
    "mark": "markPriceKlines_1m", "markPriceKlines": "markPriceKlines_1m", "markPriceKlines_1m": "markPriceKlines_1m",
    "index": "indexPriceKlines_1m", "indexPriceKlines": "indexPriceKlines_1m",
    "indexPriceKlines_1m": "indexPriceKlines_1m",
    "funding": "fundingRate", "fundingRate": "fundingRate",
    "metrics": "metrics",
    "bookDepth": "bookDepth", "book_depth": "bookDepth", "bookdepth": "bookDepth",
    "aggTrades": "aggTrades", "agg_trades": "aggTrades", "aggtrades": "aggTrades",
}

# Raw-датасеты из REST (version=2). Файл на период (день/месяц), при догрузке — слияние.
REST_KLINES = "rest_klines_1m"
REST_MARK = "rest_markPriceKlines_1m"
REST_FUNDING = "rest_fundingRate"


def resolve(names: list[str] | str) -> list[str]:
    if isinstance(names, str):
        names = [x for x in names.split(",") if x.strip()]
    out = []
    for n in names:
        key = ALIASES.get(n.strip()) or ALIASES.get(n.strip().lower())
        if key is None:
            raise ValueError(f"неизвестный датасет {n!r}; есть: {sorted(set(ALIASES))}")
        if key not in out:
            out.append(key)
    return out


def data_root(data_dir: Path | None = None) -> Path:
    return Path(data_dir) if data_dir else get_settings().data_dir


def raw_dir(dataset: str, source_symbol: str, *, source: str = SOURCE_BINANCE_UM,
            data_dir: Path | None = None) -> Path:
    return data_root(data_dir) / "raw" / source / dataset / source_symbol


def raw_path(dataset: str, source_symbol: str, period: str, *, source: str = SOURCE_BINANCE_UM,
             data_dir: Path | None = None) -> Path:
    return raw_dir(dataset, source_symbol, source=source, data_dir=data_dir) / \
        f"{source_symbol}-{dataset}-{period}.parquet"


def raw_files(dataset: str, source_symbol: str, *, source: str = SOURCE_BINANCE_UM,
              data_dir: Path | None = None) -> list[Path]:
    d = raw_dir(dataset, source_symbol, source=source, data_dir=data_dir)
    if not d.exists():
        return []
    pat = re.compile(rf"^{re.escape(source_symbol)}-{re.escape(dataset)}-(\d{{4}}-\d{{2}}(-\d{{2}})?)\.parquet$")
    return sorted(p for p in d.iterdir() if pat.match(p.name))


def period_of(path: Path) -> str:
    """'BTCUSDT-klines_1m-2024-01.parquet' → '2024-01'."""
    m = re.search(r"-(\d{4}-\d{2}(-\d{2})?)\.parquet$", path.name)
    if not m:
        raise ValueError(f"нет периода в имени {path.name}")
    return m.group(1)
