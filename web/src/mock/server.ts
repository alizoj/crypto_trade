// Маршрутизатор mock-API: эмулирует эндпоинты docs/api_contract.md в браузере.
import type {
  AccountCreate,
  AccountPatch,
  AnalysisResponse,
  ExitReason,
  JournalPatch,
  JournalQuery,
  Meta,
  PositionCreate,
  ScreenerResponse,
  Side,
  SideAnalysis,
  Signal,
  Timeframe,
} from '@/api/types'
import { MOCK_SPECS, toInstrument } from './instruments'
import { TFS, allRows, analysisText, barsResponse, contextFor, getRowCore, setupStats } from './market'
import {
  activeSignalFor,
  addPosition,
  calibration,
  closePosition,
  createAccount,
  getDb,
  iso,
  makePlan,
  markersFor,
  replaceAccount,
  reportHtml,
  signalEv,
} from './data'
import { EXIT_REASONS, dropAccountTrades, journalStats, listJournal, patchJournal } from './journal'
import { resetState } from '@/utils/reset'

export interface MockRequest {
  method: string
  path: string
  query: URLSearchParams
  body: unknown
}

export interface MockResponse {
  status: number
  body: unknown
  contentType?: string
}

const SESSION_KEY = 'hh.mock.session'

/** глобальный флаг «калибровка устарела» (/api/meta.calibration_stale); переключается из e2e */
let calibrationStale = false
export function setMockCalibrationStale(v: boolean): void {
  calibrationStale = v
}

let sessionUser: string | null = null
try {
  sessionUser = window.sessionStorage.getItem(SESSION_KEY)
} catch {
  sessionUser = null
}

export function mockSessionUser(): string | null {
  return sessionUser
}

export function setMockSession(user: string | null): void {
  sessionUser = user
  try {
    if (user) window.sessionStorage.setItem(SESSION_KEY, user)
    else window.sessionStorage.removeItem(SESSION_KEY)
  } catch {
    /* ignore */
  }
}

const json = (body: unknown, status = 200): MockResponse => ({ status, body })
const err = (status: number, detail: string): MockResponse => ({ status, body: { detail } })

function tfParam(q: URLSearchParams): Timeframe | null {
  const tf = (q.get('tf') ?? '1h') as Timeframe
  return TFS.includes(tf) ? tf : null
}

function num(v: unknown): number | undefined {
  return typeof v === 'number' && Number.isFinite(v) ? v : undefined
}

export function screenerResponse(tf: Timeframe): ScreenerResponse {
  getDb()
  return { tf, updated_at: iso(Date.now()), rows: allRows(tf).map((r) => ({ ...r })) }
}

function sideAnalysis(sym: string, tf: Timeframe, side: Side, accountId: number | null): SideAnalysis {
  const rc = getRowCore(sym, tf)!
  const row = rc.row
  const acc = accountId === null ? null : (getDb().accounts.find((a) => a.id === accountId) ?? null)
  const prob = side === 'long' ? row.p_long : row.p_short
  const active = activeSignalFor(sym, tf)
  const expires = active?.expires_at ?? iso(Date.now() + 4 * 3600e3)
  return {
    score: side === 'long' ? row.score_long : row.score_short,
    prob,
    ev_r: side === 'long' ? row.ev_long_r : row.ev_short_r,
    factors: side === 'long' ? rc.extras.factors.long : rc.extras.factors.short,
    plan: makePlan(row, side, acc, expires),
    setup_stats: setupStats(sym, tf, side, prob),
  }
}

function llmSummary(sym: string, a: SideAnalysis, b: SideAnalysis): string | null {
  // резюме «включено» только для части инструментов — как опциональный флаг конфига
  if (!['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XAUUSDT'].includes(sym)) return null
  const better = (a.score ?? 0) >= (b.score ?? 0) ? 'лонг' : 'шорт'
  return `Факторы складываются в пользу стороны «${better}»: score ${Math.max(a.score ?? 0, b.score ?? 0)} против ${Math.min(a.score ?? 0, b.score ?? 0)}. Резюме пересказывает рассчитанные факторы и не добавляет новых утверждений.`
}

export function handle(req: MockRequest): MockResponse {
  const { method, path, query } = req
  const body = (req.body ?? {}) as Record<string, unknown>

  // --- авторизация (без сессии) ---
  if (path === '/api/auth/login' && method === 'POST') {
    const username = typeof body.username === 'string' ? body.username.trim() : ''
    const password = typeof body.password === 'string' ? body.password : ''
    if (!username || !password) return err(401, 'Неверное имя пользователя или пароль')
    setMockSession(username)
    return json({ username })
  }
  if (path === '/api/auth/logout' && method === 'POST') {
    setMockSession(null)
    return json({ ok: true })
  }
  if (!sessionUser) return err(401, 'unauthorized')
  if (path === '/api/auth/me') return json({ username: sessionUser })

  const d = getDb()

  // --- мета ---
  if (path === '/api/meta') {
    const now = Date.now()
    const rs = resetState(now, { start_utc: '20:00', end_utc: '20:10' })
    const meta: Meta = {
      server_time: iso(now),
      reset_window: { start_utc: '20:00', end_utc: '20:10' },
      next_reset: iso(rs.inWindow ? rs.nextStartMs + 86400e3 : rs.nextStartMs),
      calibrated: true,
      calibration_stale: calibrationStale,
      timeframes: ['15m', '1h', '4h'],
      asset_classes: ['crypto', 'equity', 'etf', 'metal', 'energy'],
      groups: ['crypto_major', 'crypto_alt', 'tradfi'],
    }
    return json(meta)
  }

  // --- скринер ---
  if (path === '/api/screener') {
    const tf = tfParam(query)
    if (!tf) return err(422, 'tf: ожидается 15m | 1h | 4h')
    return json(screenerResponse(tf))
  }

  // --- инструменты ---
  if (path === '/api/instruments') return json(MOCK_SPECS.map(toInstrument))
  let m = /^\/api\/instruments\/([^/]+)\/(bars|analysis)$/.exec(path)
  if (m) {
    const sym = decodeURIComponent(m[1]!).toUpperCase()
    const tf = tfParam(query)
    if (!tf) return err(422, 'tf: ожидается 15m | 1h | 4h')
    if (!MOCK_SPECS.some((s) => s.sym === sym)) return err(404, `инструмент ${sym} не найден`)
    if (m[2] === 'bars') {
      const limit = Math.min(1000, Math.max(1, Number(query.get('limit') ?? 500) || 500))
      return json(barsResponse(sym, tf, limit, markersFor(sym, tf), activeSignalFor(sym, tf)))
    }
    const accRaw = query.get('account_id')
    const accountId = accRaw ? Number(accRaw) : null
    const long = sideAnalysis(sym, tf, 'long', accountId)
    const short = sideAnalysis(sym, tf, 'short', accountId)
    const rc = getRowCore(sym, tf)!
    const spec = MOCK_SPECS.find((s) => s.sym === sym)!
    const res: AnalysisResponse = {
      hh_symbol: sym,
      tf,
      ts: iso(Date.now()),
      long,
      short,
      text: analysisText(rc.row, rc.extras, tf),
      llm_summary: llmSummary(sym, long, short),
      context: contextFor(spec, tf),
    }
    return json(res)
  }

  // --- сигналы ---
  if (path === '/api/signals' && method === 'GET') {
    // status — один или несколько через запятую
    const statuses = new Set((query.get('status') ?? '').split(',').map((x) => x.trim()).filter(Boolean))
    const symbol = (query.get('symbol') ?? '').toUpperCase()
    const side = query.get('side') ?? ''
    const from = query.get('from') ?? ''
    const to = query.get('to') ?? ''
    const limit = Math.min(500, Math.max(1, Number(query.get('limit') ?? 50) || 50))
    const offset = Math.max(0, Number(query.get('offset') ?? 0) || 0)
    const fromMs = from ? Date.parse(from) : NaN
    // from — включительно, to — исключительно (ISO UTC)
    const toMs = to ? Date.parse(to) : NaN
    const items = d.signals
      .filter((s) => (!statuses.size || statuses.has(s.status)) && (!symbol || s.hh_symbol.includes(symbol)) && (!side || s.side === side))
      .filter((s) => {
        const t = Date.parse(s.created_at)
        return (Number.isNaN(fromMs) || t >= fromMs) && (Number.isNaN(toMs) || t < toMs)
      })
      .sort((a, b) => b.created_at.localeCompare(a.created_at) || b.id - a.id)
    return json({ total: items.length, items: items.slice(offset, offset + limit) })
  }
  m = /^\/api\/signals\/(\d+)\/decision$/.exec(path)
  if (m && method === 'POST') {
    const id = Number(m[1])
    const idx = d.signals.findIndex((s) => s.id === id)
    if (idx === -1) return err(404, `сигнал ${id} не найден`)
    if (typeof body.taken !== 'boolean') return err(422, 'taken: ожидается boolean')
    const sig: Signal = { ...d.signals[idx]!, taken: body.taken }
    d.signals[idx] = sig
    return json(sig)
  }
  if (path === '/api/calibration/live') {
    return json(calibration(query.get('group') ?? '', query.get('side') ?? ''))
  }

  // --- счета ---
  if (path === '/api/accounts' && method === 'GET') {
    d.accounts = d.accounts.map((a) => replaceAccount(a))
    return json(d.accounts)
  }
  if (path === '/api/accounts' && method === 'POST') {
    const b = body as Partial<AccountCreate>
    if (!b.name || (b.challenge_type !== 'two_stage' && b.challenge_type !== 'one_stage') || !num(b.size)) {
      return err(422, 'name, challenge_type, size обязательны')
    }
    return json(createAccount(b as AccountCreate))
  }
  m = /^\/api\/accounts\/(\d+)(?:\/positions(?:\/(\d+))?)?$/.exec(path)
  if (m) {
    const id = Number(m[1])
    const acc = d.accounts.find((a) => a.id === id)
    if (!acc) return err(404, `счёт ${id} не найден`)
    const isPositions = path.includes('/positions')
    if (!isPositions && method === 'PATCH') {
      const b = body as AccountPatch
      const next = { ...acc }
      if (typeof b.name === 'string' && b.name.trim()) next.name = b.name.trim()
      if (num(b.balance) !== undefined) next.balance = b.balance!
      if (b.stage) next.stage = b.stage
      if (b.status) next.status = b.status
      return json(replaceAccount(next))
    }
    if (!isPositions && method === 'DELETE') {
      d.accounts = d.accounts.filter((a) => a.id !== id)
      dropAccountTrades(id)
      return json({ ok: true })
    }
    if (isPositions && !m[2] && method === 'POST') {
      const b = body as Partial<PositionCreate>
      if (!b.hh_symbol || (b.side !== 'long' && b.side !== 'short') || !num(b.qty) || !num(b.entry_price)) {
        return err(422, 'hh_symbol, side, qty, entry_price обязательны')
      }
      // встречные позиции между счетами запрещены (раздел 2.4)
      const opposite = d.accounts.some((a) => a.id !== id && a.positions.some((p) => p.hh_symbol === b.hh_symbol && p.side !== b.side))
      if (opposite) return err(409, 'Встречная позиция на другом счёте запрещена')
      return json(addPosition(acc, b as PositionCreate))
    }
    if (isPositions && m[2] && method === 'DELETE') {
      const exit = query.get('exit_price')
      const reason = (query.get('reason') ?? 'manual') as ExitReason
      if (!EXIT_REASONS.includes(reason)) return err(422, `reason: одно из ${EXIT_REASONS.join(', ')}`)
      const res = closePosition(acc, Number(m[2]), exit ? Number(exit) : undefined, reason, query.get('note') ?? '')
      if (!res) return err(404, 'позиция не найдена')
      return json(res)
    }
  }

  // --- журнал сделок ---
  if ((path === '/api/journal' || path === '/api/journal/stats') && method === 'GET') {
    const acc = query.get('account_id')
    const q: JournalQuery = {
      account_id: acc ? Number(acc) : null,
      from: query.get('from') ?? undefined,
      to: query.get('to') ?? undefined,
      limit: Math.min(1000, Math.max(1, Number(query.get('limit') ?? 100) || 100)),
      offset: Math.max(0, Number(query.get('offset') ?? 0) || 0),
    }
    return json(path === '/api/journal' ? listJournal(q) : journalStats(q, signalEv))
  }
  m = /^\/api\/journal\/(\d+)$/.exec(path)
  if (m && method === 'PATCH') {
    const b = body as JournalPatch
    if (b.exit_reason !== undefined && !EXIT_REASONS.includes(b.exit_reason)) {
      return err(422, `exit_reason: одно из ${EXIT_REASONS.join(', ')}`)
    }
    const t = patchJournal(Number(m[1]), b)
    return t ? json(t) : err(404, 'нет сделки')
  }

  // --- бэктесты ---
  if (path === '/api/backtests') {
    const kind = query.get('kind') ?? ''
    const limit = Math.max(1, Number(query.get('limit') ?? 50) || 50)
    return json(d.backtests.filter((r) => !kind || r.kind === kind).slice(0, limit))
  }
  m = /^\/api\/backtests\/(\d+)\/report$/.exec(path)
  if (m) {
    const run = d.backtests.find((r) => r.id === Number(m![1]))
    if (!run) return err(404, 'прогон не найден')
    return { status: 200, body: reportHtml(run), contentType: 'text/html; charset=utf-8' }
  }
  if (path === '/api/trials') {
    const study = query.get('study') ?? ''
    const limit = Math.max(1, Number(query.get('limit') ?? 100) || 100)
    const items = d.trials.filter((t) => !study || t.study === study)
    return json({ total: items.length, items: items.slice(0, limit) })
  }

  return err(404, `mock: нет обработчика ${method} ${path}`)
}
