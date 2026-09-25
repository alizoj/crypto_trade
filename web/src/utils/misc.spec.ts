import { describe, expect, it } from 'vitest'
import { clockOffset, resetState } from './reset'
import { dayEndExclusiveIso, dayStartIso } from './dates'
import { TickKind, crosshairLabel, tickLabel } from './chartTime'
import { METRICS, bestRunIds } from './backtests'
import type { BacktestRun } from '@/api/types'

const W = { start_utc: '20:00', end_utc: '20:10' }
const at = (s: string) => Date.parse(s)

describe('окно дневного сброса 20:00–20:10 UTC', () => {
  it('до окна — отсчёт до 20:00 сегодня', () => {
    const r = resetState(at('2026-09-23T14:48:00Z'), W)
    expect(r.inWindow).toBe(false)
    expect(r.msToStart).toBe((5 * 60 + 12) * 60_000)
    expect(r.nextStartMs).toBe(at('2026-09-23T20:00:00Z'))
  })

  it('внутри окна — остаток окна', () => {
    const r = resetState(at('2026-09-23T20:03:30Z'), W)
    expect(r.inWindow).toBe(true)
    expect(r.msToEnd).toBe((6 * 60 + 30) * 1000)
    expect(r.nextStartMs).toBe(at('2026-09-23T20:00:00Z'))
  })

  it('граница: 20:10 — окно закончилось, следующее завтра', () => {
    const r = resetState(at('2026-09-23T20:10:00Z'), W)
    expect(r.inWindow).toBe(false)
    expect(r.nextStartMs).toBe(at('2026-09-24T20:00:00Z'))
  })

  it('окно через полночь', () => {
    const r = resetState(at('2026-09-24T00:05:00Z'), { start_utc: '23:55', end_utc: '00:10' })
    expect(r.inWindow).toBe(true)
    expect(r.msToEnd).toBe(5 * 60_000)
  })

  it('смещение часов сервера', () => {
    expect(clockOffset('2026-09-23T12:00:05Z', at('2026-09-23T12:00:00Z'))).toBe(5000)
    expect(clockOffset('bad', 0)).toBe(0)
  })
})

describe('даты фильтров журнала', () => {
  it('UTC: from — начало дня (включительно), to — начало следующего дня (исключительно)', () => {
    expect(dayStartIso('2026-09-23', 'utc')).toBe('2026-09-23T00:00:00Z')
    expect(dayEndExclusiveIso('2026-09-23', 'utc')).toBe('2026-09-24T00:00:00Z')
    expect(dayEndExclusiveIso('2026-09-30', 'utc')).toBe('2026-10-01T00:00:00Z')
    expect(dayStartIso('23.09.2026', 'utc')).toBeUndefined()
  })
  it('локальный пояс', () => {
    const expected = new Date(2026, 8, 23).toISOString().replace(/\.\d{3}Z$/, 'Z')
    expect(dayStartIso('2026-09-23', 'local')).toBe(expected)
  })
})

describe('метки времени графика', () => {
  const t = Date.parse('2026-09-23T14:30:00Z') / 1000
  it('UTC', () => {
    expect(tickLabel(t, TickKind.Time, 'utc')).toBe('14:30')
    expect(tickLabel(t, TickKind.DayOfMonth, 'utc')).toBe('23 сен')
    expect(tickLabel(t, TickKind.Month, 'utc')).toBe('сен 2026')
    expect(tickLabel(t, TickKind.Year, 'utc')).toBe('2026')
    expect(crosshairLabel(t, 'utc')).toBe('23.09.26 14:30 UTC')
  })
  it('локальное время совпадает с Date', () => {
    const d = new Date(t * 1000)
    expect(tickLabel(t, TickKind.Time, 'local')).toBe(`${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`)
  })
})

describe('сравнение прогонов', () => {
  const run = (id: number, metrics: BacktestRun['metrics']): BacktestRun => ({
    id,
    created_at: '2026-09-20T00:00:00Z',
    kind: 'backtest',
    strategy: 's',
    params_hash: 'p',
    dataset_hash: 'd',
    code_version: 'v',
    metrics,
    report_url: null,
  })
  const runs = [run(1, { p_pass_both: 0.3, pbo: 0.2, n_trades: 10 }), run(2, { p_pass_both: 0.35, pbo: 0.4, n_trades: 20 }), run(3, {})]
  const def = (k: string) => METRICS.find((m) => m.key === k)!
  it('больше — лучше / меньше — лучше / нейтрально', () => {
    expect([...bestRunIds(runs, def('p_pass_both'))]).toEqual([2])
    expect([...bestRunIds(runs, def('pbo'))]).toEqual([1])
    expect(bestRunIds(runs, def('n_trades')).size).toBe(0)
  })
  it('меньше двух значений — без подсветки', () => {
    expect(bestRunIds([runs[0]!, runs[2]!], def('p_pass_both')).size).toBe(0)
  })
})
