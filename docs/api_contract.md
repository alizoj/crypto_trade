# Контракт API (FastAPI ↔ Vue SPA)

Все пути с префиксом `/api`. JSON, UTF-8. Время — ISO 8601 UTC со `Z` (`2026-09-23T12:00:00Z`), кроме свечей графика (unix-секунды, как требует Lightweight Charts). Числа — JSON number, отсутствующее значение — `null`.

Авторизация — httpOnly cookie `hh_session` (ставит `/api/auth/login`). Любой запрос без сессии → `401 {"detail": "unauthorized"}`. SPA при 401 уводит на экран логина.

## Общие типы

```ts
type Side = "long" | "short";
type Group = "crypto_major" | "crypto_alt" | "tradfi";
type AssetClass = "crypto" | "equity" | "etf" | "metal" | "energy";

interface ProbInfo {
  p: number;            // 0..1, откалиброванная вероятность TP раньше SL
  ci_low: number;       // Wilson 95%
  ci_high: number;
  n: number;            // сделок в корзине
  calibrated: boolean;  // false → показывать score с пометкой «не откалибровано», серым
  low_data: boolean;    // n < min_n → вместо процента «мало данных», серым
}

interface TradePlan {
  side: Side;
  entry_type: "market" | "limit";
  entry: number;
  sl: number;
  sl_type: "stop_market";
  sl_trigger: "last" | "mark";
  tp1: number;
  tp2: number | null;
  tp1_frac: number;          // доля позиции на TP1
  rr1: number;
  rr2: number | null;
  account_id: number | null; // для какого счёта рассчитан размер (null — нет счёта)
  notional_usd: number | null;
  qty: number | null;
  risk_usd: number | null;
  risk_pct_daily_room: number | null;  // % от оставшегося дневного запаса
  risk_pct_total_room: number | null;
  blocked_reason: string | null;       // почему risk governor не разрешает (soft stop, лимит и т.п.)
  expires_at: string;
}
```

## Авторизация

| Метод | Путь | Тело | Ответ |
|---|---|---|---|
| POST | `/api/auth/login` | `{username, password}` | `200 {username}` + cookie; `401 {detail}` |
| POST | `/api/auth/logout` | — | `200 {ok: true}` |
| GET | `/api/auth/me` | — | `200 {username}` / `401` |

## Мета

`GET /api/meta` →
```ts
{
  server_time: string;
  reset_window: { start_utc: "20:00", end_utc: "20:10" };
  next_reset: string;             // ближайшее начало окна сброса
  calibrated: boolean;            // есть активные модели калибровки
  calibration_stale: boolean;     // live-контроль: калибровка устарела → вероятности серым
  timeframes: ["15m", "1h", "4h"];
  asset_classes: AssetClass[];
  groups: Group[];
}
```

## Скринер (экран 1)

`GET /api/screener?tf=1h` →
```ts
{ tf: string; updated_at: string; rows: ScreenerRow[] }

interface ScreenerRow {
  hh_symbol: string; base: string; asset_class: AssetClass; group: Group;
  price: number | null;
  change_24h_pct: number | null;
  htf_trend: -1 | 0 | 1;
  rsi14: number | null;
  adx: number | null;
  vwap_dist_pct: number | null;
  atr_pct: number | null;
  funding_rate: number | null;     // доля (0.0001 = 0.01%)
  oi_change_24h_pct: number | null;
  cvd_trend: -1 | 0 | 1;
  score_long: number | null;       // 0..100
  score_short: number | null;
  p_long: ProbInfo | null;
  p_short: ProbInfo | null;
  ev_long_r: number | null;
  ev_short_r: number | null;
  status: "signal" | "watch" | "none" | "off_session" | "halted";
  active_signal_id: number | null;
  active_signal_side: Side | null;
  max_per_lot_usd: number;
  max_total_usd: number;
  session_open: boolean;
  data_age_sec: number | null;     // сколько секунд назад пришёл последний бар
}
```

`WS /ws/screener?tf=1h` (cookie та же; без сессии сервер закрывает соединение с кодом 4401). Сообщения сервера:
```ts
{ type: "snapshot", tf, updated_at, rows: ScreenerRow[] }   // сразу после подключения
{ type: "update",   tf, updated_at, rows: ScreenerRow[] }   // только изменившиеся строки
{ type: "signal",   signal: Signal }                         // новый сигнал или смена статуса
{ type: "account",  account: Account }                       // изменился счёт
{ type: "ping",     ts: string }                             // раз в 20 с
```
Клиент может прислать `{ type: "tf", tf: "4h" }`, чтобы переключить ТФ без переподключения; сервер отвечает новым `snapshot` для этого ТФ.

## Инструмент (экран 2)

`GET /api/instruments` → `Instrument[]`
```ts
interface Instrument {
  hh_symbol: string; base: string; asset_class: AssetClass; group: Group; market: string;
  underlying_type: string; max_per_lot_usd: number; max_total_usd: number;
  history_start: string; binance_um: string; bybit_linear: string | null; notes: string;
}
```

`GET /api/instruments/{sym}/bars?tf=1h&limit=500&end=<ISO, опционально>` →
```ts
{
  tf: string;
  bars: { t: number; o: number; h: number; l: number; c: number; v: number }[];   // t — unix sec, открытие бара
  indicators: {
    ema20: {t: number; v: number}[]; ema50: ...; ema200: ...;
    vwap: ...; avwap: ...;
    rsi: ...;          // панель RSI
    cvd: ...;          // панель объём/CVD
    volume: {t: number; v: number; up: boolean}[];
  };
  volume_profile: { poc: number; vah: number; val: number; bins: {price: number; volume: number}[] } | null;
  markers: { t: number; side: Side; kind: "signal" | "entry" | "exit"; text: string; signal_id: number | null }[];
  active_signal: Signal | null;   // линии entry/SL/TP
}
```

`GET /api/instruments/{sym}/analysis?tf=1h&account_id=<опц.>` →
```ts
{
  hh_symbol: string; tf: string; ts: string;
  long: SideAnalysis; short: SideAnalysis;
  text: string[];                  // шаблонный разбор, 3–6 предложений
  llm_summary: string | null;      // опциональное резюме (флаг в конфиге)
  context: {
    funding: {t: number; v: number}[];
    oi: {t: number; v: number}[];
    btc_trend: -1 | 0 | 1 | null;  // для альтов
    session: { open: boolean; next_change: string | null } | null;   // для TradFi
  };
}

interface SideAnalysis {
  score: number | null;
  prob: ProbInfo | null;
  ev_r: number | null;
  factors: { name: string; label: string; value: number | string | null; points: number;
             max_points: number; verdict: "pro" | "con" | "neutral" }[];
  plan: TradePlan | null;
  setup_stats: { n: number; winrate: number | null; avg_r: number | null;
                 ci_low: number | null; ci_high: number | null;
                 last10: ("tp" | "sl" | "timeout")[] } | null;
}
```

## Журнал сигналов (экран 4)

`GET /api/signals?status=&symbol=&side=&from=&to=&limit=50&offset=0` → `{ total: number; items: Signal[] }`
(`from`/`to` — ISO UTC, `from` включительно, `to` исключительно; сортировка `created_at desc`; `status` — через запятую)
```ts
interface Signal {
  id: number; created_at: string; bar_ts: string;
  hh_symbol: string; tf: string; side: Side; strategy: string;
  score: number; prob: ProbInfo | null; ev_r: number | null;
  entry_type: "market" | "limit"; entry: number; sl: number; tp1: number; tp2: number | null;
  sl_trigger: "last" | "mark";
  status: "new" | "active" | "triggered" | "expired" | "invalidated" | "tp" | "sl" | "timeout";
  outcome_r: number | null;
  taken: boolean | null;
  reason: { pros: string[]; cons: string[] };
  plan: TradePlan | null;
  expires_at: string; closed_at: string | null;
}
```
`POST /api/signals/{id}/decision` `{taken: boolean}` → `Signal`

`GET /api/calibration/live?group=&side=` →
```ts
{ buckets: { p_pred: number; p_obs: number; n: number; ci_low: number; ci_high: number }[];
  brier: number | null; n_total: number; stale: boolean }
```

## Счета (экран 3)

`GET /api/accounts` → `Account[]`
```ts
interface Account {
  id: number; name: string; challenge_type: "two_stage" | "one_stage"; market: string;
  size: number; stage: "stage1" | "stage2" | "trader";
  balance: number; equity: number; start_of_day_balance: number;
  daily_floor: number; total_floor: number;
  daily_limit_usd: number; total_limit_usd: number;   // размеры лимитов в $ (фиксированы от размера счёта)
  daily_room_usd: number; total_room_usd: number;
  target_usd: number | null; progress_pct: number | null;
  trading_days: number; min_trading_days: number;
  soft_stop_hit: boolean; target_lock: boolean;
  status: "active" | "passed" | "failed";
  positions: Position[];
  updated_at: string;
}
interface Position {
  id: number; hh_symbol: string; side: Side; qty: number; entry_price: number;
  notional_usd: number; mark_price: number | null; unrealized_usd: number | null;
  opened_at: string; sl: number | null; tp: number | null;
}
```
| Метод | Путь | Тело | Ответ |
|---|---|---|---|
| POST | `/api/accounts` | `{name, challenge_type, size, stage?}` | `Account` |
| PATCH | `/api/accounts/{id}` | `{name?, balance?, stage?, status?}` | `Account` |
| DELETE | `/api/accounts/{id}` | — | `{ok: true}` |
| POST | `/api/accounts/{id}/positions` | `{hh_symbol, side, qty, entry_price, sl?, tp?, signal_id?}` | `Account` |
| DELETE | `/api/accounts/{id}/positions/{pos_id}?exit_price=` | — | `Account` |

## Бэктесты (экран 5)

`GET /api/backtests?kind=&limit=50` → `BacktestRun[]`
```ts
interface BacktestRun {
  id: number; created_at: string; kind: "backtest" | "walkforward" | "holdout" | "sanity";
  strategy: string; params_hash: string; dataset_hash: string; code_version: string;
  metrics: {
    p_pass_s1?: number; p_pass_s2?: number; p_pass_both?: number;
    p_fail_daily?: number; p_fail_total?: number; p_unresolved?: number;
    median_days_s1?: number; p90_days_s1?: number;
    ev_usd?: number; pf?: number; winrate?: number; avg_r?: number; n_trades?: number;
    dsr?: number; pbo?: number;
  };
  report_url: string | null;    // /api/backtests/{id}/report
}
```
`GET /api/backtests/{id}/report` → `text/html` (самодостаточный отчёт, открывать в iframe или новой вкладке)
`GET /api/trials?study=&limit=100` → `{ total: number; items: {id, created_at, study, level, strategy, group, params, metrics}[] }`
