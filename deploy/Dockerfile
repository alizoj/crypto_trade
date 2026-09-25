# Один образ для всех Python-сервисов (collector, signals, api, bot, backfill).
# Сборка SPA — отдельной стадией, результат кладётся в web/dist и отдаётся FastAPI/Caddy.

FROM node:22-alpine AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 NUMBA_CACHE_DIR=/tmp/numba
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends tini && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml ./
COPY quant ./quant
COPY signals ./signals
COPY api ./api
COPY bot ./bot
COPY collector ./collector
RUN pip install --no-cache-dir -e .
COPY config ./config
COPY --from=web /web/dist ./web/dist
RUN useradd -m quant && mkdir -p /data /reports && chown -R quant /data /reports /app
USER quant
ENV QUANT_DATA_DIR=/data QUANT_REPORTS_DIR=/reports
ENTRYPOINT ["tini", "--", "quant"]
