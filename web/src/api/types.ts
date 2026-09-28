// Типы API — строго по docs/api_contract.md.
// Время — ISO 8601 UTC со `Z`, кроме свечей и рядов графика (unix-секунды).

export type Side = 'long' | 'short'
export type Group = 'crypto_major' | 'crypto_alt' | 'tradfi'
export type AssetClass = 'crypto' | 'equity' | 'etf' | 'metal' | 'energy'
export type Timeframe = '15m' | '1h' | '4h'
export type Trend = -1 | 0 | 1

export interface ProbInfo {
  /** 0..1, откалиброванная вероятность TP раньше SL */
  p: number
  /** Wilson 95% */
  ci_low: number
  ci_high: number
  /** сделок в корзине */
  n: number
  /** false → показывать score с пометкой «не откалибровано», серым */
  calibrated: boolean
  /** n < min_n → вместо процента «мало данных», серым */
  low_data: boolean
}

export interface TradePlan {
  side: Side
  entry_type: 'market' | 'limit'
  entry: number
  sl: number
  sl_type: 'stop_market'
  sl_trigger: 'last' | 'mark'
  tp1: number
  tp2: number | null
  /** доля позиции на TP1 */
  tp1_frac: number
  rr1: number
  rr2: number | null
  /** для какого счёта рассчитан размер (null — нет счёта) */
  account_id: number | null
  notional_usd: number | null
  qty: number | null
  risk_usd: number | null
  /** % от оставшегося дневного запаса */
  risk_pct_daily_room: number | null
  risk_pct_total_room: number | null
  /** почему risk governor не разрешает (soft stop, лимит и т.п.) */
  blocked_reason: string | null
  expires_at: string
}

// ---------- Авторизация ----------

export interface LoginRequest {
  username: string
  password: string
}

export interface UserInfo {
  username: string
}

// ---------- Мета ----------

export interface Meta {
  server_time: string
  reset_window: { start_utc: string; end_utc: string }
  /** ближайшее начало окна сброса */
  next_reset: string
  /** есть активные модели калибровки */
  calibrated: boolean
  /** live-контроль: калибровка устарела → вероятности серым */
  calibration_stale: boolean
  timeframes: Timeframe[]
  asset_classes: AssetClass[]
  groups: Group[]
}

// ---------- Скринер ----------

export type ScreenerStatus = 'signal' | 'watch' | 'none' | 'off_session' | 'halted'

export interface ScreenerRow {
  hh_symbol: string
  base: string
  asset_class: AssetClass
  group: Group
  price: number | null
  change_24h_pct: number | null
  htf_trend: Trend
  rsi14: number | null
  adx: number | null
  vwap_dist_pct: number | null
  atr_pct: number | null
  /** доля (0.0001 = 0.01%) */
  funding_rate: number | null
  oi_change_24h_pct: number | null
  cvd_trend: Trend
  /** 0..100 */
  score_long: number | null
  score_short: number | null
  p_long: ProbInfo | null
  p_short: ProbInfo | null
  ev_long_r: number | null
  ev_short_r: number | null
  status: ScreenerStatus
  active_signal_id: number | null
  active_signal_side: Side | null
  max_per_lot_usd: number
  max_total_usd: number
  session_open: boolean
  /** сколько секунд назад пришёл последний бар */
  data_age_sec: number | null
}

export interface ScreenerResponse {
  tf: string
  updated_at: string
  rows: ScreenerRow[]
}

// ---------- WebSocket /ws/screener ----------

export type WsServerMessage =
  | { type: 'snapshot'; tf: string; updated_at: string; rows: ScreenerRow[] }
  | { type: 'update'; tf: string; updated_at: string; rows: ScreenerRow[] }
  | { type: 'signal'; signal: Signal }
  | { type: 'account'; account: Account }
  | { type: 'ping'; ts: string }

/** Клиент → сервер: смена ТФ без переподключения; сервер отвечает snapshot нового ТФ. */
export type WsClientMessage = { type: 'tf'; tf: Timeframe }

/** Код закрытия WS при отсутствии сессии. */
export const WS_CLOSE_UNAUTHORIZED = 4401

// ---------- Инструмент ----------

export interface Instrument {
  hh_symbol: string
  base: string
  asset_class: AssetClass
  group: Group
  market: string
  underlying_type: string
  max_per_lot_usd: number
  max_total_usd: number
  history_start: string
  binance_um: string
  bybit_linear: string | null
  notes: string
}

export interface Bar {
  /** unix sec, открытие бара */
  t: number
  o: number
  h: number
  l: number
  c: number
  v: number
}

export interface TimeValue {
  t: number
  v: number
}

export interface VolumePoint {
  t: number
  v: number
  up: boolean
}

export type MarkerKind = 'signal' | 'entry' | 'exit'

export interface ChartMarker {
  t: number
  side: Side
  kind: MarkerKind
  text: string
  signal_id: number | null
}

export interface VolumeProfile {
  poc: number
  vah: number
  val: number
  bins: { price: number; volume: number }[]
}

export interface BarsResponse {
  tf: string
  bars: Bar[]
  indicators: {
    ema20: TimeValue[]
    ema50: TimeValue[]
    ema200: TimeValue[]
    vwap: TimeValue[]
    avwap: TimeValue[]
    rsi: TimeValue[]
    cvd: TimeValue[]
    volume: VolumePoint[]
  }
  volume_profile: VolumeProfile | null
  markers: ChartMarker[]
  /** линии entry/SL/TP */
  active_signal: Signal | null
}

export type FactorVerdict = 'pro' | 'con' | 'neutral'

export interface Factor {
  name: string
  label: string
  value: number | string | null
  points: number
  max_points: number
  verdict: FactorVerdict
}

export type SetupOutcome = 'tp' | 'sl' | 'timeout'

export interface SetupStats {
  n: number
  winrate: number | null
  avg_r: number | null
  ci_low: number | null
  ci_high: number | null
  last10: SetupOutcome[]
}

export interface SideAnalysis {
  score: number | null
  prob: ProbInfo | null
  ev_r: number | null
  factors: Factor[]
  plan: TradePlan | null
  setup_stats: SetupStats | null
}

export interface AnalysisResponse {
  hh_symbol: string
  tf: string
  ts: string
  long: SideAnalysis
  short: SideAnalysis
  /** шаблонный разбор, 3–6 предложений */
  text: string[]
  /** опциональное резюме (флаг в конфиге) */
  llm_summary: string | null
  context: {
    funding: TimeValue[]
    oi: TimeValue[]
    /** для альтов */
    btc_trend: Trend | null
    /** для TradFi */
    session: { open: boolean; next_change: string | null } | null
  }
}

// ---------- Журнал сигналов ----------

export type SignalStatus =
  | 'new'
  | 'active'
  | 'triggered'
  | 'expired'
  | 'invalidated'
  | 'tp'
  | 'sl'
  | 'timeout'

export interface Signal {
  id: number
  created_at: string
  bar_ts: string
  hh_symbol: string
  tf: string
  side: Side
  strategy: string
  score: number
  prob: ProbInfo | null
  ev_r: number | null
  entry_type: 'market' | 'limit'
  entry: number
  sl: number
  tp1: number
  tp2: number | null
  sl_trigger: 'last' | 'mark'
  status: SignalStatus
  outcome_r: number | null
  taken: boolean | null
  reason: { pros: string[]; cons: string[] }
  plan: TradePlan | null
  expires_at: string
  closed_at: string | null
}

export interface SignalsQuery {
  /** один статус или несколько (уходят через запятую) */
  status?: SignalStatus | SignalStatus[] | ''
  symbol?: string
  side?: Side | ''
  /** ISO UTC, включительно */
  from?: string
  /** ISO UTC, исключительно */
  to?: string
  limit?: number
  offset?: number
}

export interface SignalsResponse {
  total: number
  items: Signal[]
}

export interface CalibrationBucket {
  p_pred: number
  p_obs: number
  n: number
  ci_low: number
  ci_high: number
}

export interface CalibrationLive {
  buckets: CalibrationBucket[]
  brier: number | null
  n_total: number
  stale: boolean
}

// ---------- Счета ----------

export type ChallengeType = 'two_stage' | 'one_stage'
export type Stage = 'stage1' | 'stage2' | 'trader'
export type AccountStatus = 'active' | 'passed' | 'failed'

export interface Position {
  id: number
  hh_symbol: string
  side: Side
  qty: number
  entry_price: number
  notional_usd: number
  mark_price: number | null
  unrealized_usd: number | null
  opened_at: string
  sl: number | null
  tp: number | null
}

export interface Account {
  id: number
  name: string
  challenge_type: ChallengeType
  market: string
  size: number
  stage: Stage
  balance: number
  equity: number
  start_of_day_balance: number
  daily_floor: number
  total_floor: number
  /** размеры лимитов в $ (фиксированы от размера счёта) */
  daily_limit_usd: number
  total_limit_usd: number
  daily_room_usd: number
  total_room_usd: number
  target_usd: number | null
  progress_pct: number | null
  trading_days: number
  min_trading_days: number
  soft_stop_hit: boolean
  target_lock: boolean
  status: AccountStatus
  positions: Position[]
  updated_at: string
  /** правило 6% Элдера; null — нет данных */
  month: MonthStatus | null
}

/** Правило 6% Элдера: убыток с начала месяца + риск открытых позиций <= limit_pct баланса на начало месяца. */
export interface MonthStatus {
  /** YYYY-MM по проп-календарю (00:00 UTC+4) */
  month: string
  start_balance: number
  /** баланс − баланс на начало месяца */
  result_usd: number
  open_risk_usd: number
  limit_pct: number
  limit_usd: number
  used_usd: number
  /** null — правило выключено */
  room_usd: number | null
  enabled: boolean
  blocked: boolean
}

// ---------- Журнал сделок ----------

export type ExitReason = 'manual' | 'sl' | 'tp' | 'timeout' | 'other'

export interface JournalTrade {
  id: number
  account_id: number
  hh_symbol: string
  side: Side
  qty: number
  entry_price: number
  exit_price: number
  sl: number | null
  tp: number | null
  opened_at: string
  closed_at: string
  signal_id: number | null
  strategy: string
  fees_usd: number
  /** за вычетом комиссий входа и выхода */
  pnl_usd: number
  /** qty × |вход − SL|; без стопа — null */
  risk_usd: number | null
  r_multiple: number | null
  exit_reason: ExitReason
  note: string
}

export interface JournalResponse {
  total: number
  items: JournalTrade[]
}

export interface JournalBlock {
  n: number
  wins: number
  win_rate: number | null
  pnl_usd: number
  fees_usd: number
  n_r: number
  sum_r: number | null
  avg_r: number | null
  avg_win_r: number | null
  avg_loss_r: number | null
  profit_factor: number | null
  max_loss_streak: number
  /** средний ev_r сигналов, по которым открыты сделки */
  expected_avg_r: number | null
  n_expected: number
  from_signals: number
  without_stop: number
  /** убыток больше 1.1R — стоп не соблюдён */
  stop_breaches: number
  first_closed_at: string | null
  last_closed_at: string | null
}

export interface JournalSeries extends JournalBlock {
  index: number
  complete: boolean
}

export interface JournalStats {
  series_size: number
  total: JournalBlock
  series: JournalSeries[]
}

export interface JournalQuery {
  account_id?: number | null
  from?: string
  to?: string
  limit?: number
  offset?: number
}

export interface JournalPatch {
  note?: string
  exit_reason?: ExitReason
}

export interface AccountCreate {
  name: string
  challenge_type: ChallengeType
  size: number
  stage?: Stage
}

export interface AccountPatch {
  name?: string
  balance?: number
  stage?: Stage
  status?: AccountStatus
}

export interface PositionCreate {
  hh_symbol: string
  side: Side
  qty: number
  entry_price: number
  sl?: number
  tp?: number
  signal_id?: number
}

// ---------- Бэктесты ----------

export type BacktestKind = 'backtest' | 'walkforward' | 'holdout' | 'sanity'

export interface BacktestMetrics {
  p_pass_s1?: number
  p_pass_s2?: number
  p_pass_both?: number
  p_fail_daily?: number
  p_fail_total?: number
  p_unresolved?: number
  median_days_s1?: number
  p90_days_s1?: number
  ev_usd?: number
  pf?: number
  winrate?: number
  avg_r?: number
  n_trades?: number
  dsr?: number
  pbo?: number
}

export interface BacktestRun {
  id: number
  created_at: string
  kind: BacktestKind
  strategy: string
  params_hash: string
  dataset_hash: string
  code_version: string
  metrics: BacktestMetrics
  /** /api/backtests/{id}/report */
  report_url: string | null
}

export interface Trial {
  id: number
  created_at: string
  study: string
  level: string | number
  strategy: string
  group: string
  params: Record<string, unknown>
  metrics: Record<string, unknown>
}

export interface TrialsResponse {
  total: number
  items: Trial[]
}

export interface OkResponse {
  ok: true
}
