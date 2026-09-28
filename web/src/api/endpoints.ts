// Типизированные обёртки эндпоинтов из docs/api_contract.md.
import { http, request } from './client'
import type {
  Account,
  AccountCreate,
  AccountPatch,
  AnalysisResponse,
  BacktestKind,
  BacktestRun,
  BarsResponse,
  CalibrationLive,
  ExitReason,
  Group,
  Instrument,
  JournalPatch,
  JournalQuery,
  JournalResponse,
  JournalStats,
  JournalTrade,
  LoginRequest,
  Meta,
  OkResponse,
  PositionCreate,
  ScreenerResponse,
  Side,
  Signal,
  SignalsQuery,
  SignalsResponse,
  Timeframe,
  TrialsResponse,
  UserInfo,
} from './types'

const enc = encodeURIComponent

export const api = {
  // --- auth ---
  login: (body: LoginRequest) =>
    request<UserInfo>('/api/auth/login', { method: 'POST', body, skipAuthRedirect: true }),
  logout: () => request<OkResponse>('/api/auth/logout', { method: 'POST', skipAuthRedirect: true }),
  me: () => request<UserInfo>('/api/auth/me', { skipAuthRedirect: true }),

  // --- meta ---
  meta: () => http.get<Meta>('/api/meta'),

  // --- screener ---
  screener: (tf: Timeframe) => http.get<ScreenerResponse>('/api/screener', { tf }),

  // --- instruments ---
  instruments: () => http.get<Instrument[]>('/api/instruments'),
  bars: (sym: string, tf: Timeframe, limit = 500, end?: string, signal?: AbortSignal) =>
    http.get<BarsResponse>(`/api/instruments/${enc(sym)}/bars`, { tf, limit, end }, signal),
  analysis: (sym: string, tf: Timeframe, accountId?: number | null, signal?: AbortSignal) =>
    http.get<AnalysisResponse>(
      `/api/instruments/${enc(sym)}/analysis`,
      { tf, account_id: accountId ?? undefined },
      signal,
    ),

  // --- signals ---
  signals: (q: SignalsQuery = {}) =>
    http.get<SignalsResponse>('/api/signals', {
      status: Array.isArray(q.status) ? q.status.join(',') : q.status,
      symbol: q.symbol,
      side: q.side,
      from: q.from,
      to: q.to,
      limit: q.limit ?? 50,
      offset: q.offset ?? 0,
    }),
  decision: (id: number, taken: boolean) => http.post<Signal>(`/api/signals/${id}/decision`, { taken }),
  calibrationLive: (group?: Group | '', side?: Side | '') =>
    http.get<CalibrationLive>('/api/calibration/live', { group, side }),

  // --- accounts ---
  accounts: () => http.get<Account[]>('/api/accounts'),
  createAccount: (body: AccountCreate) => http.post<Account>('/api/accounts', body),
  patchAccount: (id: number, body: AccountPatch) => http.patch<Account>(`/api/accounts/${id}`, body),
  deleteAccount: (id: number) => http.del<OkResponse>(`/api/accounts/${id}`),
  addPosition: (id: number, body: PositionCreate) => http.post<Account>(`/api/accounts/${id}/positions`, body),
  closePosition: (id: number, posId: number, exitPrice?: number, reason?: ExitReason, note?: string) =>
    http.del<Account>(`/api/accounts/${id}/positions/${posId}`, { exit_price: exitPrice, reason, note: note || undefined }),

  // --- journal (Элдер: таблица и дневник; Дуглас: серии по 20 сделок) ---
  journal: (q: JournalQuery = {}) =>
    http.get<JournalResponse>('/api/journal', {
      account_id: q.account_id ?? undefined,
      from: q.from,
      to: q.to,
      limit: q.limit ?? 100,
      offset: q.offset ?? 0,
    }),
  journalStats: (q: JournalQuery = {}) =>
    http.get<JournalStats>('/api/journal/stats', { account_id: q.account_id ?? undefined, from: q.from, to: q.to }),
  patchJournal: (id: number, body: JournalPatch) => http.patch<JournalTrade>(`/api/journal/${id}`, body),

  // --- backtests ---
  backtests: (kind?: BacktestKind | '', limit = 50) => http.get<BacktestRun[]>('/api/backtests', { kind, limit }),
  backtestReport: (url: string) => request<string>(url, { as: 'text' }),
  trials: (study?: string, limit = 100) => http.get<TrialsResponse>('/api/trials', { study, limit }),
}
