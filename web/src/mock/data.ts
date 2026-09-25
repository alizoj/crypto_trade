// «База данных» mock-режима: сигналы, счета, бэктесты, реестр испытаний, калибровка.
import type {
  Account,
  AccountCreate,
  BacktestRun,
  CalibrationLive,
  ChallengeType,
  ChartMarker,
  Group,
  Position,
  PositionCreate,
  ScreenerRow,
  Side,
  Signal,
  SignalStatus,
  Stage,
  Timeframe,
  TradePlan,
  Trial,
} from '@/api/types'
import { MOCK_SPECS } from './instruments'
import {
  FEES_R,
  MIN_PROB,
  RR1,
  RR2,
  TFS,
  TF_SEC,
  allRows,
  ensureSignals,
  getRowCore,
  getSeries,
  setRowStatus,
  signalSideFor,
} from './market'
import { hashSeed, pick, rng, round, roundPrice, wilson } from './math'

export function iso(ms: number): string {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')
}

// ---------------- правила челленджей (раздел 2.1) ----------------

interface StageRules {
  target: number | null
  daily: number
  total: number
  minDays: number
}

export function stageRules(type: ChallengeType, stage: Stage): StageRules {
  if (type === 'one_stage') {
    return stage === 'trader' ? { target: null, daily: 0.03, total: 0.06, minDays: 0 } : { target: 0.1, daily: 0.03, total: 0.06, minDays: 3 }
  }
  if (stage === 'stage1') return { target: 0.08, daily: 0.05, total: 0.1, minDays: 5 }
  if (stage === 'stage2') return { target: 0.06, daily: 0.05, total: 0.08, minDays: 5 }
  return { target: null, daily: 0.05, total: 0.08, minDays: 0 }
}

/** Доля использованного дневного лимита, после которой срабатывает soft stop (параметр risk governor). */
const SOFT_STOP_SHARE = 0.6
const TAKER_FEE = 0.0003

function markPrice(sym: string): number | null {
  return getRowCore(sym, '1h')?.row.price ?? null
}

export function recalcAccount(a: Account): Account {
  const rules = stageRules(a.challenge_type, a.stage)
  const positions: Position[] = a.positions.map((p) => {
    const mark = markPrice(p.hh_symbol)
    const dir = p.side === 'long' ? 1 : -1
    return {
      ...p,
      mark_price: mark,
      unrealized_usd: mark === null ? null : round((mark - p.entry_price) * p.qty * dir, 2),
    }
  })
  const unreal = positions.reduce((s, p) => s + (p.unrealized_usd ?? 0), 0)
  const equity = round(a.balance + unreal, 2)
  const dailyLimit = a.size * rules.daily
  const dailyFloor = round(a.start_of_day_balance - dailyLimit, 2)
  const totalFloor = round(a.size * (1 - rules.total), 2)
  const dailyRoom = round(equity - dailyFloor, 2)
  const target = rules.target === null ? null : round(a.size * (1 + rules.target), 2)
  const progress = target === null ? null : round(((a.balance - a.size) / (target - a.size)) * 100, 2)
  return {
    ...a,
    positions,
    equity,
    daily_floor: dailyFloor,
    total_floor: totalFloor,
    daily_limit_usd: round(dailyLimit, 2),
    total_limit_usd: round(a.size * rules.total, 2),
    daily_room_usd: dailyRoom,
    total_room_usd: round(equity - totalFloor, 2),
    target_usd: target,
    progress_pct: progress,
    min_trading_days: rules.minDays,
    soft_stop_hit: dailyLimit - dailyRoom >= dailyLimit * SOFT_STOP_SHARE,
    target_lock: target !== null && a.balance >= target && a.trading_days < rules.minDays,
    updated_at: iso(Date.now()),
  }
}

// ---------------- план сделки ----------------

export function makePlan(row: ScreenerRow, side: Side, acc: Account | null, expiresAt: string, entryOverride?: number): TradePlan {
  const entry = entryOverride ?? row.price ?? 0
  const atrAbs = ((row.atr_pct ?? 1) / 100) * entry
  const slDist = atrAbs * 1.2
  const dir = side === 'long' ? 1 : -1
  const prob = side === 'long' ? row.p_long : row.p_short
  const plan: TradePlan = {
    side,
    entry_type: row.vwap_dist_pct !== null && Math.abs(row.vwap_dist_pct) > 0.4 ? 'limit' : 'market',
    entry: roundPrice(entry),
    sl: roundPrice(entry - dir * slDist),
    sl_type: 'stop_market',
    sl_trigger: 'last',
    tp1: roundPrice(entry + dir * slDist * RR1),
    tp2: roundPrice(entry + dir * slDist * RR2),
    tp1_frac: 0.5,
    rr1: RR1,
    rr2: RR2,
    account_id: acc?.id ?? null,
    notional_usd: null,
    qty: null,
    risk_usd: null,
    risk_pct_daily_room: null,
    risk_pct_total_room: null,
    blocked_reason: null,
    expires_at: expiresAt,
  }
  if (acc) {
    let budget = Math.min(Math.max(0, acc.daily_room_usd) * 0.2, acc.size * 0.005)
    if (acc.target_lock) budget = acc.size * 0.001
    let qty = slDist > 0 ? budget / slDist : 0
    const cap = Math.min(row.max_per_lot_usd, acc.equity * 5)
    if (qty * entry > cap) qty = cap / entry
    const notional = qty * entry
    const risk = qty * slDist + notional * TAKER_FEE * 2
    plan.qty = round(qty, qty >= 100 ? 0 : qty >= 1 ? 3 : 6)
    plan.notional_usd = round(notional, 2)
    plan.risk_usd = round(risk, 2)
    plan.risk_pct_daily_room = acc.daily_room_usd > 0 ? round((risk / acc.daily_room_usd) * 100, 2) : null
    plan.risk_pct_total_room = acc.total_room_usd > 0 ? round((risk / acc.total_room_usd) * 100, 2) : null
  }
  let reason: string | null = null
  if (row.status === 'halted') reason = 'Торги приостановлены (halted): сигналы по замороженным барам не строятся'
  else if (row.status === 'off_session') reason = 'Вне сессии базового рынка (tradfi_off_session_signals: false)'
  else if (!prob || !prob.calibrated) reason = 'Вероятность не откалибрована — сигнал не создаётся'
  else if (prob.low_data) reason = `Мало данных: n = ${prob.n} < min_n (100)`
  else if (prob.p < MIN_PROB) reason = `P = ${(prob.p * 100).toFixed(0)}% ниже порога min_prob (${Math.round(MIN_PROB * 100)}%)`
  else if (acc && acc.status !== 'active') reason = 'Счёт не активен'
  else if (acc && acc.soft_stop_hit) reason = 'Soft daily stop: новые входы заблокированы до ресета 20:00 UTC'
  plan.blocked_reason = reason
  return plan
}

// ---------------- состояние ----------------

interface Db {
  signals: Signal[]
  accounts: Account[]
  nextAccountId: number
  nextPositionId: number
  backtests: BacktestRun[]
  trials: Trial[]
}

let db: Db | null = null

function simulateOutcome(sym: string, tf: Timeframe, i: number, side: Side, entry: number, slDist: number): { status: SignalStatus; outcome: number | null; exitIdx: number | null } {
  const s = getSeries(sym, tf)!
  const dir = side === 'long' ? 1 : -1
  const sl = entry - dir * slDist
  const tp = entry + dir * slDist * RR1
  const maxHold = 48
  for (let k = i + 1; k < Math.min(s.bars.length - 1, i + 1 + maxHold); k++) {
    const b = s.bars[k]!
    const hitSl = side === 'long' ? b.l <= sl : b.h >= sl
    const hitTp = side === 'long' ? b.h >= tp : b.l <= tp
    if (hitSl) return { status: 'sl', outcome: round(-1 - FEES_R, 2), exitIdx: k } // консервативно: SL первым
    if (hitTp) return { status: 'tp', outcome: round(RR1 - FEES_R, 2), exitIdx: k }
  }
  const endIdx = Math.min(s.bars.length - 2, i + maxHold)
  if (endIdx <= i + 1) return { status: 'active', outcome: null, exitIdx: null }
  const endC = s.bars[endIdx]!.c
  return { status: 'timeout', outcome: round(((endC - entry) * dir) / slDist - FEES_R, 2), exitIdx: endIdx }
}

function buildHistory(r: () => number): Signal[] {
  const out: Signal[] = []
  const syms = MOCK_SPECS.filter((s) => s.cls === 'crypto' || s.cls === 'metal' || s.cls === 'energy').map((s) => s.sym)
  const strategies = ['confluence_v1', 'confluence_v1', 'confluence_v1', 'baseline_random']
  for (let k = 0; k < 150; k++) {
    const sym = pick(r, syms)
    const tf = pick(r, TFS)
    const s = getSeries(sym, tf)!
    const i = 260 + Math.floor(r() * (s.bars.length - 320))
    const bar = s.bars[i]!
    const tfSec = TF_SEC[tf]
    const side: Side = r() < 0.55 ? 'long' : 'short'
    const entry = bar.c
    const slDist = entry * (0.004 + r() * 0.012)
    const p = round(0.5 + r() * 0.2, 4)
    const n = 120 + Math.floor(r() * 600)
    const [lo, hi] = wilson(p, n)
    const createdMs = (bar.t + tfSec) * 1000 + 3000 + Math.floor(r() * 4000)
    const expired = r() < 0.12
    const invalidated = !expired && r() < 0.06
    let status: SignalStatus
    let outcome: number | null = null
    let closedMs: number | null = null
    if (expired || invalidated) {
      status = expired ? 'expired' : 'invalidated'
      closedMs = createdMs + tfSec * 3 * 1000
    } else {
      const sim = simulateOutcome(sym, tf, i, side, entry, slDist)
      status = sim.status
      outcome = sim.outcome
      closedMs = sim.exitIdx !== null ? (s.bars[sim.exitIdx]!.t + tfSec) * 1000 : null
    }
    const dir = side === 'long' ? 1 : -1
    const expires = iso(createdMs + tfSec * 4 * 1000)
    const takenRoll = r()
    out.push({
      id: 0,
      created_at: iso(createdMs),
      bar_ts: iso(bar.t * 1000),
      hh_symbol: sym,
      tf,
      side,
      strategy: pick(r, strategies),
      score: Math.round(55 + r() * 35),
      prob: { p, ci_low: round(lo, 4), ci_high: round(hi, 4), n, calibrated: true, low_data: false },
      ev_r: round(p * RR1 - (1 - p) - FEES_R, 3),
      entry_type: r() < 0.6 ? 'limit' : 'market',
      entry: roundPrice(entry),
      sl: roundPrice(entry - dir * slDist),
      tp1: roundPrice(entry + dir * slDist * RR1),
      tp2: roundPrice(entry + dir * slDist * RR2),
      sl_trigger: r() < 0.8 ? 'last' : 'mark',
      status,
      outcome_r: outcome,
      taken: takenRoll < 0.35 ? true : takenRoll < 0.7 ? false : null,
      reason: {
        pros: ['HTF-тренд по направлению сделки', pick(r, ['Откат к AVWAP', 'Снятие ликвидности', 'BOS по направлению']), pick(r, ['CVD растёт', 'Скрытая дивергенция RSI', 'Объём выше среднего'])],
        cons: [pick(r, ['ADX < 20 — тренд слабый', 'Funding повышен', 'BTC у сопротивления', 'Близко к VAH'])],
      },
      plan: null,
      expires_at: expires,
      closed_at: closedMs === null ? null : iso(closedMs),
    })
  }
  out.sort((a, b) => a.created_at.localeCompare(b.created_at))
  out.forEach((s, idx) => (s.id = idx + 1))
  return out
}

function buildActive(accounts: Account[], startId: number): Signal[] {
  const out: Signal[] = []
  let id = startId
  const acc = accounts[0] ?? null
  for (const tf of TFS) {
    ensureSignals(tf, 3)
    const tfSec = TF_SEC[tf]
    for (const row of allRows(tf)) {
      const side = signalSideFor(row)
      if (!side) continue
      const s = getSeries(row.hh_symbol, tf)!
      const closed = s.bars[s.bars.length - 2]!
      const createdMs = (closed.t + tfSec) * 1000 + 4000
      const expires = iso(createdMs + tfSec * 4 * 1000)
      const plan = makePlan(row, side, acc, expires, closed.c)
      const prob = side === 'long' ? row.p_long : row.p_short
      const ext = getRowCore(row.hh_symbol, tf)!.extras
      const factors = side === 'long' ? ext.factors.long : ext.factors.short
      const sig: Signal = {
        id: id++,
        created_at: iso(createdMs),
        bar_ts: iso(closed.t * 1000),
        hh_symbol: row.hh_symbol,
        tf,
        side,
        strategy: 'confluence_v1',
        score: (side === 'long' ? row.score_long : row.score_short) ?? 0,
        prob,
        ev_r: side === 'long' ? row.ev_long_r : row.ev_short_r,
        entry_type: plan.entry_type,
        entry: plan.entry,
        sl: plan.sl,
        tp1: plan.tp1,
        tp2: plan.tp2,
        sl_trigger: plan.sl_trigger,
        status: out.length % 3 === 0 ? 'new' : 'active',
        outcome_r: null,
        taken: null,
        reason: {
          pros: factors.filter((f) => f.verdict === 'pro').map((f) => `${f.label}: ${f.value}`),
          cons: factors.filter((f) => f.verdict === 'con').map((f) => `${f.label}: ${f.value}`),
        },
        plan,
        expires_at: expires,
        closed_at: null,
      }
      out.push(sig)
      setRowStatus(row.hh_symbol, tf, 'signal', sig.id, side)
    }
  }
  return out
}

function baseAccounts(): Account[] {
  const now = iso(Date.now())
  const sol = markPrice('SOLUSDT') ?? 200
  const xau = markPrice('XAUUSDT') ?? 3700
  const eth = markPrice('ETHUSDT') ?? 4000
  const a1: Account = {
    id: 1,
    name: 'Boost 10k — основной',
    challenge_type: 'two_stage',
    market: 'crypto_tradfi',
    size: 10000,
    stage: 'stage1',
    balance: 10312.4,
    equity: 0,
    start_of_day_balance: 10290,
    daily_floor: 0,
    total_floor: 0,
    daily_limit_usd: 0,
    total_limit_usd: 0,
    daily_room_usd: 0,
    total_room_usd: 0,
    target_usd: null,
    progress_pct: null,
    trading_days: 3,
    min_trading_days: 5,
    soft_stop_hit: false,
    target_lock: false,
    status: 'active',
    positions: [
      { id: 1, hh_symbol: 'SOLUSDT', side: 'long', qty: 12, entry_price: roundPrice(sol * 0.994), notional_usd: round(12 * sol * 0.994, 2), mark_price: null, unrealized_usd: null, opened_at: iso(Date.now() - 5 * 3600e3), sl: roundPrice(sol * 0.975), tp: roundPrice(sol * 1.023) },
      { id: 2, hh_symbol: 'XAUUSDT', side: 'short', qty: 0.6, entry_price: roundPrice(xau * 1.003), notional_usd: round(0.6 * xau * 1.003, 2), mark_price: null, unrealized_usd: null, opened_at: iso(Date.now() - 2 * 3600e3), sl: roundPrice(xau * 1.012), tp: roundPrice(xau * 0.99) },
    ],
    updated_at: now,
  }
  const a2: Account = {
    id: 2,
    name: 'One-stage 25k',
    challenge_type: 'one_stage',
    market: 'crypto_tradfi',
    size: 25000,
    stage: 'stage1',
    balance: 27610.5,
    equity: 0,
    start_of_day_balance: 27580,
    daily_floor: 0,
    total_floor: 0,
    daily_limit_usd: 0,
    total_limit_usd: 0,
    daily_room_usd: 0,
    total_room_usd: 0,
    target_usd: null,
    progress_pct: null,
    trading_days: 2,
    min_trading_days: 3,
    soft_stop_hit: false,
    target_lock: false,
    status: 'active',
    positions: [
      { id: 3, hh_symbol: 'ETHUSDT', side: 'long', qty: 0.2, entry_price: roundPrice(eth * 0.998), notional_usd: round(0.2 * eth * 0.998, 2), mark_price: null, unrealized_usd: null, opened_at: iso(Date.now() - 50 * 60e3), sl: roundPrice(eth * 0.985), tp: null },
    ],
    updated_at: now,
  }
  return [recalcAccount(a1), recalcAccount(a2)]
}

function buildBacktests(): BacktestRun[] {
  const day = 86400e3
  const now = Date.now()
  const mk = (id: number, kind: BacktestRun['kind'], strategy: string, ago: number, metrics: BacktestRun['metrics']): BacktestRun => ({
    id,
    created_at: iso(now - ago * day),
    kind,
    strategy,
    params_hash: (hashSeed(`p${id}`) >>> 0).toString(16).padStart(8, '0'),
    dataset_hash: (hashSeed(`d${id}`) >>> 0).toString(16).padStart(8, '0'),
    code_version: `v0.${id - 95}.0-${(hashSeed(`c${id}`) >>> 0).toString(16).slice(0, 7)}`,
    metrics,
    report_url: `/api/backtests/${id}/report`,
  })
  return [
    mk(103, 'holdout', 'confluence_v1', 1, { p_pass_s1: 0.552, p_pass_s2: 0.508, p_pass_both: 0.281, p_fail_daily: 0.138, p_fail_total: 0.203, p_unresolved: 0.127, median_days_s1: 19, p90_days_s1: 49, ev_usd: 164.2, pf: 1.21, winrate: 0.521, avg_r: 0.12, n_trades: 610, dsr: 0.84, pbo: 0.22 }),
    mk(102, 'walkforward', 'confluence_v1', 4, { p_pass_s1: 0.583, p_pass_s2: 0.541, p_pass_both: 0.315, p_fail_daily: 0.121, p_fail_total: 0.187, p_unresolved: 0.109, median_days_s1: 17, p90_days_s1: 44, ev_usd: 212.4, pf: 1.28, winrate: 0.534, avg_r: 0.16, n_trades: 2140, dsr: 0.91, pbo: 0.18 }),
    mk(101, 'backtest', 'baseline_random', 9, { p_pass_s1: 0.412, p_pass_s2: 0.388, p_pass_both: 0.171, p_fail_daily: 0.214, p_fail_total: 0.297, p_unresolved: 0.118, median_days_s1: 23, p90_days_s1: 61, ev_usd: -38.5, pf: 0.97, winrate: 0.487, avg_r: -0.02, n_trades: 5120, dsr: 0.03, pbo: 0.61 }),
  ]
}

function buildTrials(r: () => number): Trial[] {
  const out: Trial[] = []
  const groups: Group[] = ['crypto_major', 'crypto_alt', 'tradfi']
  const now = Date.now()
  for (let i = 0; i < 84; i++) {
    const study = i < 60 ? 'confluence_v1_entries' : 'confluence_v1_exits'
    const pass = round(0.12 + r() * 0.22, 3)
    out.push({
      id: 5000 + i,
      created_at: iso(now - (84 - i) * 3 * 3600e3),
      study,
      level: study.endsWith('entries') ? 1 : 2,
      strategy: 'confluence_v1',
      group: pick(r, groups),
      params:
        study === 'confluence_v1_entries'
          ? { min_score: 55 + Math.floor(r() * 25), rsi_len: pick(r, [9, 14, 21]), htf: pick(r, ['1h', '4h']) }
          : { sl_atr: round(0.8 + r() * 1.2, 2), tp1_r: pick(r, [1.2, 1.5, 2]), tp1_frac: pick(r, [0.3, 0.5, 0.7]) },
      metrics: { p_pass_both: pass, ev_usd: round((pass - 0.18) * 1200, 1), n_trades: 300 + Math.floor(r() * 2500), winrate: round(0.45 + r() * 0.12, 3) },
    })
  }
  return out.reverse()
}

export function getDb(): Db {
  if (db) return db
  const r = rng(20260923)
  // прогреваем строки всех ТФ
  for (const tf of TFS) allRows(tf)
  const accounts = baseAccounts()
  const history = buildHistory(r)
  const active = buildActive(accounts, 1001)
  db = {
    signals: [...history, ...active],
    accounts,
    nextAccountId: 3,
    nextPositionId: 10,
    backtests: buildBacktests(),
    trials: buildTrials(r),
  }
  return db
}

// ---------------- операции ----------------

export function markersFor(sym: string, tf: Timeframe): ChartMarker[] {
  const d = getDb()
  const tfSec = TF_SEC[tf]
  const out: ChartMarker[] = []
  for (const s of d.signals) {
    if (s.hh_symbol !== sym || s.tf !== tf) continue
    const t = Math.floor(Date.parse(s.bar_ts) / 1000)
    const pTxt = s.prob ? ` ${Math.round(s.prob.p * 100)}%` : ''
    out.push({ t, side: s.side, kind: 'signal', text: `${s.side === 'long' ? 'L' : 'S'}${pTxt}`, signal_id: s.id })
    if (['triggered', 'tp', 'sl', 'timeout'].includes(s.status)) {
      out.push({ t: t + tfSec, side: s.side, kind: 'entry', text: 'вход', signal_id: s.id })
    }
    if (s.closed_at && ['tp', 'sl', 'timeout'].includes(s.status)) {
      const ct = Math.floor(Date.parse(s.closed_at) / 1000 / tfSec) * tfSec - tfSec
      out.push({ t: ct, side: s.side, kind: 'exit', text: `${s.status.toUpperCase()} ${s.outcome_r !== null ? (s.outcome_r > 0 ? '+' : '') + s.outcome_r.toFixed(2) + 'R' : ''}`.trim(), signal_id: s.id })
    }
  }
  return out
}

export function activeSignalFor(sym: string, tf: Timeframe): Signal | null {
  const row = getRowCore(sym, tf)?.row
  if (!row?.active_signal_id) return null
  return getDb().signals.find((s) => s.id === row.active_signal_id) ?? null
}

export function createAccount(body: AccountCreate): Account {
  const d = getDb()
  const acc: Account = recalcAccount({
    id: d.nextAccountId++,
    name: body.name,
    challenge_type: body.challenge_type,
    market: 'crypto_tradfi',
    size: body.size,
    stage: body.stage ?? 'stage1',
    balance: body.size,
    equity: body.size,
    start_of_day_balance: body.size,
    daily_floor: 0,
    total_floor: 0,
    daily_limit_usd: 0,
    total_limit_usd: 0,
    daily_room_usd: 0,
    total_room_usd: 0,
    target_usd: null,
    progress_pct: null,
    trading_days: 0,
    min_trading_days: 0,
    soft_stop_hit: false,
    target_lock: false,
    status: 'active',
    positions: [],
    updated_at: iso(Date.now()),
  })
  d.accounts.push(acc)
  return acc
}

export function replaceAccount(acc: Account): Account {
  const d = getDb()
  const fresh = recalcAccount(acc)
  d.accounts = d.accounts.map((a) => (a.id === acc.id ? fresh : a))
  return fresh
}

export function addPosition(acc: Account, body: PositionCreate): Account {
  const d = getDb()
  const pos: Position = {
    id: d.nextPositionId++,
    hh_symbol: body.hh_symbol,
    side: body.side,
    qty: body.qty,
    entry_price: body.entry_price,
    notional_usd: round(body.qty * body.entry_price, 2),
    mark_price: null,
    unrealized_usd: null,
    opened_at: iso(Date.now()),
    sl: body.sl ?? null,
    tp: body.tp ?? null,
  }
  const fee = pos.notional_usd * TAKER_FEE
  return replaceAccount({ ...acc, balance: round(acc.balance - fee, 2), positions: [...acc.positions, pos] })
}

export function closePosition(acc: Account, posId: number, exitPrice?: number): Account | null {
  const pos = acc.positions.find((p) => p.id === posId)
  if (!pos) return null
  const exit = exitPrice ?? pos.mark_price ?? pos.entry_price
  const dir = pos.side === 'long' ? 1 : -1
  const pnl = (exit - pos.entry_price) * pos.qty * dir - exit * pos.qty * TAKER_FEE
  return replaceAccount({ ...acc, balance: round(acc.balance + pnl, 2), positions: acc.positions.filter((p) => p.id !== posId) })
}

export function calibration(group: string, side: string): CalibrationLive {
  const r = rng(hashSeed(`calib|${group}|${side}`))
  const buckets = []
  let nTotal = 0
  let brierAcc = 0
  for (let i = 0; i < 8; i++) {
    const pPred = round(0.375 + i * 0.05, 3)
    const n = Math.max(8, Math.round((group === 'tradfi' ? 12 : 70) * (1 - i * 0.09) * (0.7 + r() * 0.6)))
    const pObs = round(Math.min(0.98, Math.max(0.02, pPred + (r() - 0.5) * (group === 'tradfi' ? 0.25 : 0.09))), 3)
    const [lo, hi] = wilson(pObs, n)
    buckets.push({ p_pred: pPred, p_obs: pObs, n, ci_low: round(lo, 3), ci_high: round(hi, 3) })
    nTotal += n
    brierAcc += n * (pObs * (1 - pPred) ** 2 + (1 - pObs) * pPred ** 2)
  }
  return { buckets, brier: round(brierAcc / nTotal, 4), n_total: nTotal, stale: group === 'tradfi' }
}

export function reportHtml(run: BacktestRun): string {
  const r = rng(run.id)
  let eq = 10000
  const pts: string[] = []
  for (let i = 0; i < 120; i++) {
    eq += (r() - 0.5 + (run.metrics.avg_r ?? 0) * 0.6) * 60
    pts.push(`${(i * 5).toFixed(0)},${(200 - (eq - 9000) / 15).toFixed(1)}`)
  }
  const rows = Object.entries(run.metrics)
    .map(([k, v]) => `<tr><td>${k}</td><td style="text-align:right">${v}</td></tr>`)
    .join('')
  return `<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Отчёт #${run.id}</title>
<style>body{font-family:system-ui,sans-serif;margin:24px;color:#0f172a;background:#fff}table{border-collapse:collapse}td{border:1px solid #d9dee7;padding:4px 10px}h1{font-size:20px}.muted{color:#5b6678}</style>
</head><body><h1>Отчёт прогона #${run.id} — ${run.strategy} (${run.kind})</h1>
<p class="muted">Mock-отчёт. Датасет ${run.dataset_hash}, параметры ${run.params_hash}, код ${run.code_version}.</p>
<h2>Кривая equity (Monte Carlo, медиана)</h2>
<svg width="600" height="220" style="border:1px solid #d9dee7"><polyline fill="none" stroke="#2563eb" stroke-width="2" points="${pts.join(' ')}"/></svg>
<h2>Метрики</h2><table>${rows}</table></body></html>`
}
