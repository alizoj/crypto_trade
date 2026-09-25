"""Метрики Prometheus (раздел 4: лаг данных по символу, гэпы, реконнекты, отставание потребителя)."""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

BARS = Counter("quant_bars_total", "Закрытые 1m-бары, полученные коллектором", ["source"])
RECONNECTS = Counter("quant_ws_reconnects_total", "Переподключения WebSocket", ["source"])
DATA_LAG = Gauge("quant_data_lag_seconds", "Сколько секунд назад закрылся последний бар", ["hh_symbol"])
RECOMPUTE = Histogram("quant_screener_recompute_seconds", "Пересчёт строки скринера", ["tf"],
                      buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2))
SIGNALS = Counter("quant_signals_total", "Созданные сигналы", ["side", "tf"])
CONSUMER_LAG = Gauge("quant_consumer_lag_seconds", "Задержка обработки бара сервисом сигналов")
