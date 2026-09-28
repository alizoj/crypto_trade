# HashHedge Quant

Личная система мониторинга, бэктеста и live-сигналов под правила проп-компании HashHedge.
Спецификация — [docs/TZ_hashhedge_quant_system.md](docs/TZ_hashhedge_quant_system.md), контракт API — [docs/api_contract.md](docs/api_contract.md).
Система не торгует сама: исполнение ручное, она считает и подсказывает.

## Установка (Windows / Linux)

```bash
python -m venv .venv
.venv/Scripts/python -m pip install -e ".[dev]"      # Linux: .venv/bin/python
cd web && npm install && npm run build && cd ..        # веб-интерфейс → web/dist
```

Без Docker всё работает локально: данные — Parquet в `data/`, БД — SQLite `data/quant.db`, шина — в памяти.
Для прода — `deploy/docker-compose.yml` (PostgreSQL, Redis Streams, ClickHouse, Caddy, Prometheus/Grafana).

## Фазы и команды

| Фаза | Что | Команда |
|---|---|---|
| 1 | правила HashHedge (propsim) | `pytest tests/propsim` |
| 2 | проверка инструментов по биржам | `quant instruments validate` |
| 2 | загрузка архива data.binance.vision | `quant data backfill --symbols BTCUSDT,ETHUSDT --datasets klines,mark,funding,metrics --start 2022-01` |
| 2 | сборка нормализованного слоя + флаги качества | `quant data build --symbols ...` |
| 2 | качество данных, гэпы, догрузка | `quant data quality`, `quant data status`, `quant data fill-gaps` |
| 3 | индикаторы и стратегии | `pytest tests/indicators tests/strategies` |
| 4 | санити-чек модели | `quant sanity` (`--quick` — быстро) |
| 4 | бэктест по правилам челленджа | `quant backtest run --symbols BTCUSDT,ETHUSDT --start 2024-01-01` |
| 4 | уровень 1: скрининг гипотез | `quant research screen --groups crypto_major --start 2023-01-01` |
| 4 | выходы по MFE/MAE | `quant research exits --symbols BTCUSDT --start 2024-01-01` |
| 5 | walk-forward (purge/embargo, DSR/PBO) | `quant optimize walkforward --groups crypto_major --group-label crypto_major --start 2022-01-01` |
| 5 | однократная проверка на hold-out | `quant optimize holdout --wf-run <id>` |
| 6 | калибровка score → P | `quant calibrate fit --wf-run <id>`, `quant calibrate check --run <holdout_id>` |
| 6–7 | всё live в одном процессе | `quant auth create-user --username me` → `quant live run` → http://127.0.0.1:8000 |
| 6–7 | то же в фоне | `quant live start` / `quant live status` / `quant live stop` (лог — `data/logs/live.log`) |
| 7 | веб-интерфейс без бэкенда | `cd web && npm run dev:mock` (любое имя и пароль) |

Отчёты — `reports_out/run_XXXXX/report.html` и экран «Бэктесты» в интерфейсе.

## Live-режим

`quant live run` поднимает коллектор Binance (WebSocket; если сеть не пропускает фьючерсный WS —
автоматически REST-поллинг раз в минуту), сервис сигналов, API с веб-интерфейсом и Telegram-бота
(если задан `QUANT_TELEGRAM_TOKEN`). Настройки — переменные `QUANT_*` или файл `.env`
(см. `quant/config.py`, `deploy/.env.example`).

Вероятности в интерфейсе появляются только после калибровки на out-of-sample сделках walk-forward
(`quant calibrate fit`). До этого показывается score с пометкой «не откалибровано», а сигналы
не создаются (`live.require_calibration` в конфиге стратегии).

**Журнал сделок** (экран «Сделки», бот `/journal`). Каждая закрытая позиция записывается с результатом
в $ за вычетом комиссий, в R от начального стопа, причиной выхода и заметкой. Это таблица и дневник
трейдера по Элдеру. Сделки группируются в серии по 20 (Дуглас: судить о системе по выборке, а не по
последней сделке). По каждой серии видна дисциплина: сделки вне сигналов, сделки без стопа и убытки
больше заранее принятого риска.

**Правило 6% Элдера** (`risk.monthly_loss_pct: 0.06`, только live-план). Если убыток с начала месяца
плюс риск открытых позиций достиг 6% баланса на начало месяца, план сделки не даёт новых входов до
конца месяца. Месяц считается по проп-календарю (00:00 UTC+4). Симулятор бэктеста это правило
не моделирует. Значение 0 выключает правило.

## Структура

```
quant/            ядро: config, instruments, data, indicators, strategies, propsim, research, backtest,
                  montecarlo, optimize, calibration, reports, analysis, risk, live, bus, db
signals/          сервис сигналов (тот же код стратегий, что в бэктесте)
collector/        Binance WS / REST-резерв, резервный Bybit
api/              FastAPI: REST + WebSocket + раздача SPA
bot/              Telegram-бот
web/              Vue 3 + TS SPA
config/           challenge.yaml, instruments.csv, strategies/*.yaml
deploy/           docker-compose, Dockerfile, ClickHouse DDL, Caddy, Prometheus, Grafana
tests/            pytest (propsim, данные, индикаторы, стратегии, бэктест, калибровка, API)
```
