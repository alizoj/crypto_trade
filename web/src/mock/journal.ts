// Журнал сделок mock-режима: те же формулы, что quant/live/journal.py (серии по 20, правило 6% Элдера).
import type {
  Account,
  ExitReason,
  JournalBlock,
  JournalPatch,
  JournalQuery,
  JournalResponse,
  JournalStats,
  JournalTrade,
  MonthStatus,
  Position,
  Side,
} from '@/api/types'
import { hashSeed, pick, rng, round } from './math'

export const EXIT_REASONS: ExitReason[] = ['manual', 'sl', 'tp', 'timeout', 'other']
export const MONTH_PCT = 0.06
const SERIES = 20
const STOP_BREACH_R = -1.1
const TAKER_FEE = 0.0003

function isoOf(ms: number): string {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')
}

/** Месяц проп-календаря: день начинается в 00:00 UTC+4. */
export function monthKey(ms: number): string {
  const d = new Date(ms + 4 * 3600e3)
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, '0')}`
}

const monthStarts = new Map<number, { month: string; start: number }>()

export function setMonthStart(accountId: number, start: number, now = Date.now()): void {
  monthStarts.set(accountId, { month: monthKey(now), start })
}

function positionRisk(p: Position): number {
  if (p.sl !== null) return p.qty * Math.abs(p.entry_price - p.sl)
  return Math.max(0, -(p.unrealized_usd ?? 0))
}

export function monthStatus(a: Pick<Account, 'id' | 'balance' | 'positions'>, now = Date.now()): MonthStatus {
  const key = monthKey(now)
  let m = monthStarts.get(a.id)
  if (!m || m.month !== key) {
    m = { month: key, start: a.balance }
    monthStarts.set(a.id, m)
  }
  const result = a.balance - m.start
  const openRisk = a.positions.reduce((s, p) => s + positionRisk(p), 0)
  const limit = MONTH_PCT * m.start
  const used = Math.max(0, -result) + openRisk
  return {
    month: m.month,
    start_balance: round(m.start, 2),
    result_usd: round(result, 2),
    open_risk_usd: round(openRisk, 2),
    limit_pct: MONTH_PCT,
    limit_usd: round(limit, 2),
    used_usd: round(used, 2),
    room_usd: round(limit - used, 2),
    enabled: true,
    blocked: used >= limit,
  }
}

// ---------------- сделки ----------------

let trades: JournalTrade[] = []
let nextId = 1

export function recordClose(acc: Account, pos: Position, exit: number, reason: ExitReason, note: string): JournalTrade {
  const dir = pos.side === 'long' ? 1 : -1
  const fees = pos.qty * (pos.entry_price + exit) * TAKER_FEE
  const pnl = dir * pos.qty * (exit - pos.entry_price) - fees
  const risk = pos.sl === null ? null : pos.qty * Math.abs(pos.entry_price - pos.sl)
  const t: JournalTrade = {
    id: nextId++,
    account_id: acc.id,
    hh_symbol: pos.hh_symbol,
    side: pos.side,
    qty: pos.qty,
    entry_price: pos.entry_price,
    exit_price: exit,
    sl: pos.sl,
    tp: pos.tp,
    opened_at: pos.opened_at,
    closed_at: isoOf(Date.now()),
    signal_id: null,
    strategy: '',
    fees_usd: round(fees, 4),
    pnl_usd: round(pnl, 2),
    risk_usd: risk === null ? null : round(risk, 4),
    r_multiple: risk ? round(pnl / risk, 4) : null,
    exit_reason: reason,
    note,
  }
  trades.push(t)
  return t
}

export function dropAccountTrades(accountId: number): void {
  trades = trades.filter((t) => t.account_id !== accountId)
}

function filtered(q: JournalQuery): JournalTrade[] {
  const from = q.from ? Date.parse(q.from) : NaN
  const to = q.to ? Date.parse(q.to) : NaN
  return trades.filter((t) => {
    const c = Date.parse(t.closed_at)
    return (q.account_id == null || t.account_id === q.account_id) && (Number.isNaN(from) || c >= from) && (Number.isNaN(to) || c < to)
  })
}

export function listJournal(q: JournalQuery): JournalResponse {
  const items = filtered(q).sort((a, b) => b.closed_at.localeCompare(a.closed_at) || b.id - a.id)
  const offset = q.offset ?? 0
  return { total: items.length, items: items.slice(offset, offset + (q.limit ?? 100)) }
}

export function patchJournal(id: number, body: JournalPatch): JournalTrade | null {
  const idx = trades.findIndex((t) => t.id === id)
  if (idx === -1) return null
  const next: JournalTrade = { ...trades[idx]! }
  if (typeof body.note === 'string') next.note = body.note
  if (body.exit_reason) next.exit_reason = body.exit_reason
  trades[idx] = next
  return next
}

function block(ts: JournalTrade[], evOf: (id: number) => number | null): JournalBlock {
  const rs = ts.map((t) => t.r_multiple).filter((r): r is number => r !== null)
  const wins = ts.filter((t) => t.pnl_usd > 0).length
  const gain = ts.filter((t) => t.pnl_usd > 0).reduce((s, t) => s + t.pnl_usd, 0)
  const loss = -ts.filter((t) => t.pnl_usd < 0).reduce((s, t) => s + t.pnl_usd, 0)
  let streak = 0
  let worst = 0
  for (const t of ts) {
    streak = t.pnl_usd < 0 ? streak + 1 : 0
    worst = Math.max(worst, streak)
  }
  const exp = ts.map((t) => (t.signal_id === null ? null : evOf(t.signal_id))).filter((x): x is number => x !== null)
  const winR = rs.filter((r) => r > 0)
  const lossR = rs.filter((r) => r <= 0)
  const mean = (xs: number[]) => (xs.length ? xs.reduce((s, x) => s + x, 0) / xs.length : null)
  return {
    n: ts.length,
    wins,
    win_rate: ts.length ? wins / ts.length : null,
    pnl_usd: round(ts.reduce((s, t) => s + t.pnl_usd, 0), 2),
    fees_usd: round(ts.reduce((s, t) => s + t.fees_usd, 0), 2),
    n_r: rs.length,
    sum_r: rs.length ? rs.reduce((s, r) => s + r, 0) : null,
    avg_r: mean(rs),
    avg_win_r: mean(winR),
    avg_loss_r: mean(lossR),
    profit_factor: loss > 0 ? gain / loss : null,
    max_loss_streak: worst,
    expected_avg_r: mean(exp),
    n_expected: exp.length,
    from_signals: ts.filter((t) => t.signal_id !== null).length,
    without_stop: ts.filter((t) => t.sl === null).length,
    stop_breaches: ts.filter((t) => t.r_multiple !== null && t.r_multiple < STOP_BREACH_R).length,
    first_closed_at: ts[0]?.closed_at ?? null,
    last_closed_at: ts[ts.length - 1]?.closed_at ?? null,
  }
}

export function journalStats(q: JournalQuery, evOf: (id: number) => number | null = () => null): JournalStats {
  const ts = filtered(q).sort((a, b) => a.closed_at.localeCompare(b.closed_at) || a.id - b.id)
  const series = []
  for (let k = 0; k < ts.length; k += SERIES) {
    const chunk = ts.slice(k, k + SERIES)
    series.push({ index: k / SERIES + 1, complete: chunk.length === SERIES, ...block(chunk, evOf) })
  }
  return { series_size: SERIES, total: block(ts, evOf), series }
}

// ---------------- фикстуры ----------------

const NOTES = [
  'по сигналу, всё по плану',
  'закрыл раньше цели — испугался отката',
  'вход раньше сигнала (страх упустить)',
  '',
  'стоп на месте, убыток — плата за проверку',
  'перенёс стоп дальше — ошибка',
  '',
  'частичная фиксация 1/3, остаток по стопу в б/у',
]

/** История закрытых сделок: 27 по основному счёту (серия 1 завершена, серия 2 идёт) и 8 по второму. */
export function seedJournal(accounts: Account[], priceOf: (sym: string) => number | null, signalIds: number[]): void {
  trades = []
  nextId = 1
  const r = rng(hashSeed('journal'))
  const syms = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'XAUUSDT', 'BNBUSDT', 'AVAXUSDT']
  const now = Date.now()
  const plan: [number, number][] = [
    [accounts[0]?.id ?? 1, 27],
    [accounts[1]?.id ?? 2, 8],
  ]
  for (const [accId, count] of plan) {
    for (let i = 0; i < count; i++) {
      const sym = pick(r, syms)
      const px = priceOf(sym) ?? 100
      const side: Side = r() < 0.5 ? 'long' : 'short'
      const dir = side === 'long' ? 1 : -1
      const entry = round(px * (1 + (r() - 0.5) * 0.06), 2)
      const noStop = i % 13 === 7
      const stopDist = entry * (0.008 + r() * 0.012)
      const sl = noStop ? null : round(entry - dir * stopDist, 2)
      const win = r() < 0.46
      let rMult = win ? 0.8 + r() * 1.7 : -(0.9 + r() * 0.15)
      if (i === 11) rMult = -1.6                     // перенёс стоп — убыток больше риска
      const exit = round(entry + dir * rMult * stopDist, 2)
      const qty = round(80 / stopDist, 4)
      const closed = now - (count - i) * 17 * 3600e3 - Math.floor(r() * 3600e3)
      const fees = qty * (entry + exit) * TAKER_FEE
      const pnl = dir * qty * (exit - entry) - fees
      const risk = sl === null ? null : qty * Math.abs(entry - sl)
      const sig = i % 3 === 0 && signalIds.length ? signalIds[i % signalIds.length]! : null
      trades.push({
        id: nextId++,
        account_id: accId,
        hh_symbol: sym,
        side,
        qty,
        entry_price: entry,
        exit_price: exit,
        sl,
        tp: sl === null ? null : round(entry + dir * 2 * stopDist, 2),
        opened_at: isoOf(closed - (2 + Math.floor(r() * 30)) * 3600e3),
        closed_at: isoOf(closed),
        signal_id: sig,
        strategy: sig === null ? '' : 'confluence_v1',
        fees_usd: round(fees, 4),
        pnl_usd: round(pnl, 2),
        risk_usd: risk === null ? null : round(risk, 4),
        r_multiple: risk ? round(pnl / risk, 4) : null,
        exit_reason: win ? (r() < 0.6 ? 'tp' : 'manual') : i === 11 ? 'manual' : 'sl',
        note: i === 11 ? NOTES[5]! : pick(r, NOTES),
      })
    }
  }
}
