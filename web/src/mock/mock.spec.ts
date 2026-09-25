// Фикстуры mock-режима: объём и разнообразие данных, соответствие контракту.
import { beforeAll, describe, expect, it } from 'vitest'
import type { Account, BacktestRun, BarsResponse, Instrument, ScreenerResponse, SignalsResponse, AnalysisResponse } from '@/api/types'
import { handle, setMockCalibrationStale, setMockSession } from './server'

function call<T>(method: string, url: string, body?: unknown): { status: number; body: T } {
  const u = new URL(url, 'http://localhost')
  const res = handle({ method, path: u.pathname, query: u.searchParams, body })
  return { status: res.status, body: res.body as T }
}

describe('mock: авторизация и мета', () => {
  it('meta.calibration_stale', () => {
    setMockSession('u')
    expect(call<{ calibration_stale: boolean }>('GET', '/api/meta').body.calibration_stale).toBe(false)
    setMockCalibrationStale(true)
    expect(call<{ calibration_stale: boolean }>('GET', '/api/meta').body.calibration_stale).toBe(true)
    setMockCalibrationStale(false)
  })

  it('без сессии — 401, вход по любому имени и паролю', () => {
    setMockSession(null)
    expect(call('GET', '/api/screener?tf=1h').status).toBe(401)
    expect(call('POST', '/api/auth/login', { username: 'u', password: '' }).status).toBe(401)
    expect(call('POST', '/api/auth/login', { username: 'u', password: 'p' }).status).toBe(200)
    expect(call<{ username: string }>('GET', '/api/auth/me').body.username).toBe('u')
  })
})

describe('mock: фикстуры', () => {
  beforeAll(() => setMockSession('tester'))

  it('≥ 30 инструментов разных классов', () => {
    const list = call<Instrument[]>('GET', '/api/instruments').body
    expect(list.length).toBeGreaterThanOrEqual(30)
    expect(new Set(list.map((i) => i.asset_class))).toEqual(new Set(['crypto', 'equity', 'etf', 'metal', 'energy']))
    expect(new Set(list.map((i) => i.group))).toEqual(new Set(['crypto_major', 'crypto_alt', 'tradfi']))
  })

  it('скринер: есть сигналы, low_data, не откалибровано, off_session, halted', () => {
    for (const tf of ['15m', '1h', '4h']) {
      const res = call<ScreenerResponse>('GET', `/api/screener?tf=${tf}`).body
      expect(res.tf).toBe(tf)
      expect(res.rows.length).toBeGreaterThanOrEqual(30)
      const signals = res.rows.filter((r) => r.status === 'signal')
      expect(signals.length).toBeGreaterThanOrEqual(3)
      expect(signals.every((r) => r.active_signal_id !== null && r.active_signal_side !== null)).toBe(true)
      expect(res.rows.filter((r) => r.status !== 'signal').every((r) => r.active_signal_side === null)).toBe(true)
      expect(res.rows.some((r) => r.p_long?.low_data || r.p_short?.low_data)).toBe(true)
      expect(res.rows.some((r) => r.p_long?.calibrated === false)).toBe(true)
      expect(res.rows.some((r) => r.status === 'off_session' && !r.session_open)).toBe(true)
      expect(res.rows.some((r) => r.status === 'halted')).toBe(true)
      for (const r of res.rows) {
        for (const p of [r.p_long, r.p_short]) {
          if (!p) continue
          expect(p.ci_low).toBeLessThanOrEqual(p.p)
          expect(p.ci_high).toBeGreaterThanOrEqual(p.p)
          expect(p.low_data).toBe(p.n < 100)
        }
      }
    }
  })

  it('цена одинакова на всех ТФ (последняя цена), RSI различается', () => {
    const a = call<ScreenerResponse>('GET', '/api/screener?tf=1h').body.rows.find((r) => r.hh_symbol === 'BTCUSDT')!
    const b = call<ScreenerResponse>('GET', '/api/screener?tf=4h').body.rows.find((r) => r.hh_symbol === 'BTCUSDT')!
    expect(a.price).toBe(b.price)
    expect(a.rsi14).not.toBe(b.rsi14)
  })

  it('бары ≥ 300, индикаторы и профиль объёма, последняя свеча = цена скринера', () => {
    const res = call<BarsResponse>('GET', '/api/instruments/ETHUSDT/bars?tf=1h&limit=500').body
    expect(res.bars.length).toBe(500)
    expect(res.indicators.ema200.length).toBe(500)
    expect(res.indicators.rsi.length).toBeGreaterThan(400)
    expect(res.volume_profile!.val).toBeLessThanOrEqual(res.volume_profile!.poc)
    expect(res.volume_profile!.vah).toBeGreaterThanOrEqual(res.volume_profile!.poc)
    for (let i = 1; i < res.bars.length; i++) expect(res.bars[i]!.t).toBeGreaterThan(res.bars[i - 1]!.t)
    for (const b of res.bars) {
      expect(b.h).toBeGreaterThanOrEqual(Math.max(b.o, b.c))
      expect(b.l).toBeLessThanOrEqual(Math.min(b.o, b.c))
    }
    const row = call<ScreenerResponse>('GET', '/api/screener?tf=1h').body.rows.find((r) => r.hh_symbol === 'ETHUSDT')!
    expect(res.bars.at(-1)!.c).toBe(row.price)
  })

  it('разбор: score = сумма баллов факторов, план со stop-market SL', () => {
    const res = call<AnalysisResponse>('GET', '/api/instruments/BTCUSDT/analysis?tf=1h&account_id=1').body
    for (const side of ['long', 'short'] as const) {
      const a = res[side]
      expect(a.score).toBe(a.factors.reduce((s, f) => s + f.points, 0))
      expect(a.plan!.sl_type).toBe('stop_market')
      expect(a.plan!.account_id).toBe(1)
      expect(a.setup_stats!.last10).toHaveLength(10)
    }
    expect(res.text.length).toBeGreaterThanOrEqual(3)
    expect(res.text.length).toBeLessThanOrEqual(6)
  })

  it('журнал: пагинация, фильтры, решение', () => {
    const all = call<SignalsResponse>('GET', '/api/signals?limit=50&offset=0').body
    expect(all.total).toBeGreaterThan(100)
    expect(all.items).toHaveLength(50)
    const p2 = call<SignalsResponse>('GET', '/api/signals?limit=50&offset=50').body
    expect(p2.items[0]!.id).not.toBe(all.items[0]!.id)
    const tp = call<SignalsResponse>('GET', '/api/signals?status=tp&limit=500').body
    expect(tp.items.every((s) => s.status === 'tp' && (s.outcome_r ?? 0) > 0)).toBe(true)
    // status через запятую
    const closed = call<SignalsResponse>('GET', '/api/signals?status=tp,sl&limit=500').body
    expect(closed.items.every((s) => s.status === 'tp' || s.status === 'sl')).toBe(true)
    expect(closed.total).toBeGreaterThan(tp.total)
    // сортировка created_at desc
    for (let i = 1; i < all.items.length; i++) expect(all.items[i - 1]!.created_at >= all.items[i]!.created_at).toBe(true)
    // from — включительно, to — исключительно
    const t0 = all.items[5]!.created_at
    expect(call<SignalsResponse>('GET', '/api/signals?from=' + t0 + '&to=' + t0 + '&limit=500').body.total).toBe(0)
    const incl = call<SignalsResponse>('GET', '/api/signals?from=' + t0 + '&limit=500').body
    expect(incl.items.some((s) => s.created_at === t0)).toBe(true)
    const excl = call<SignalsResponse>('GET', '/api/signals?to=' + t0 + '&limit=500').body
    expect(excl.items.every((s) => s.created_at < t0)).toBe(true)
    const id = all.items[0]!.id
    expect(call<{ taken: boolean }>('POST', `/api/signals/${id}/decision`, { taken: true }).body.taken).toBe(true)
  })

  it('2 счёта; ручной ввод баланса пересчитывает equity и запас', () => {
    const accs = call<Account[]>('GET', '/api/accounts').body
    expect(accs).toHaveLength(2)
    expect(accs.some((a) => a.target_lock)).toBe(true)
    const a = accs[0]!
    const upd = call<Account>('PATCH', `/api/accounts/${a.id}`, { balance: a.balance - 100 }).body
    expect(upd.balance).toBeCloseTo(a.balance - 100, 2)
    expect(upd.daily_room_usd).toBeCloseTo(upd.equity - upd.daily_floor, 2)
    // лимиты в $ фиксированы от размера счёта: 5% / 10% для стадии 1 двухстадийного, 3% / 6% — одностадийного
    expect(upd.daily_limit_usd).toBe(a.size * 0.05)
    expect(upd.total_limit_usd).toBe(a.size * 0.1)
    expect(accs[1]!.daily_limit_usd).toBe(accs[1]!.size * 0.03)
    expect(accs[1]!.total_limit_usd).toBe(accs[1]!.size * 0.06)
  })

  it('3 прогона бэктеста с HTML-отчётом, реестр испытаний', () => {
    const runs = call<BacktestRun[]>('GET', '/api/backtests').body
    expect(runs).toHaveLength(3)
    const rep = handle({ method: 'GET', path: runs[0]!.report_url!, query: new URLSearchParams(), body: undefined })
    expect(rep.contentType).toContain('text/html')
    expect(String(rep.body)).toContain('<html')
    expect(call<{ total: number }>('GET', '/api/trials?limit=10').body.total).toBeGreaterThan(50)
  })
})
