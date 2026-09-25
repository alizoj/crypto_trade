import { describe, expect, it } from 'vitest'
import type { ProbInfo, ScreenerRow } from '@/api/types'
import {
  DEFAULT_FILTERS,
  dominantSide,
  filterRows,
  isDefaultFilters,
  rowMatches,
  sanitizeFilters,
  sanitizePresets,
  upsertPreset,
  usableP,
  type ScreenerFilters,
} from './screenerFilters'

const P = (p: number, over: Partial<ProbInfo> = {}): ProbInfo => ({ p, ci_low: p - 0.05, ci_high: p + 0.05, n: 300, calibrated: true, low_data: false, ...over })

function row(sym: string, over: Partial<ScreenerRow> = {}): ScreenerRow {
  return {
    hh_symbol: sym,
    base: sym.replace('USDT', ''),
    asset_class: 'crypto',
    group: 'crypto_major',
    price: 100,
    change_24h_pct: 1,
    htf_trend: 1,
    rsi14: 50,
    adx: 25,
    vwap_dist_pct: 0.1,
    atr_pct: 1,
    funding_rate: 0.0001,
    oi_change_24h_pct: 2,
    cvd_trend: 1,
    score_long: 60,
    score_short: 40,
    p_long: P(0.6),
    p_short: P(0.45),
    ev_long_r: 0.4,
    ev_short_r: 0.05,
    status: 'watch',
    active_signal_id: null,
    active_signal_side: null,
    max_per_lot_usd: 100000,
    max_total_usd: 500000,
    session_open: true,
    data_age_sec: 5,
    ...over,
  }
}

const F = (over: Partial<ScreenerFilters> = {}): ScreenerFilters => ({ ...DEFAULT_FILTERS, classes: [], ...over })

const rows: ScreenerRow[] = [
  row('BTCUSDT', { status: 'signal', active_signal_id: 1, active_signal_side: 'long' }),
  row('ETHUSDT', { score_long: 30, score_short: 70, p_long: P(0.4), p_short: P(0.62), ev_long_r: -0.1, ev_short_r: 0.45, status: 'signal', active_signal_id: 2, active_signal_side: 'short' }),
  row('XAUUSDT', { asset_class: 'metal', group: 'tradfi', p_long: P(0.7, { low_data: true, n: 40 }), ev_long_r: 0.7 }),
  row('TSLAUSDT', { asset_class: 'equity', group: 'tradfi', p_long: P(0.66, { calibrated: false }), status: 'off_session' }),
  row('SOLUSDT', { score_long: 50, score_short: 50 }),
]

describe('фильтры скринера', () => {
  it('по умолчанию — все строки', () => {
    expect(filterRows(rows, F())).toHaveLength(rows.length)
    expect(isDefaultFilters(F())).toBe(true)
  })

  it('класс актива (несколько)', () => {
    expect(filterRows(rows, F({ classes: ['metal'] })).map((r) => r.hh_symbol)).toEqual(['XAUUSDT'])
    expect(filterRows(rows, F({ classes: ['metal', 'equity'] }))).toHaveLength(2)
  })

  it('только сигналы', () => {
    expect(filterRows(rows, F({ onlySignals: true })).map((r) => r.hh_symbol)).toEqual(['BTCUSDT', 'ETHUSDT'])
  })

  it('min P учитывает только откалиброванные P с достаточным n', () => {
    const res = filterRows(rows, F({ minP: 60 })).map((r) => r.hh_symbol)
    expect(res).toEqual(['BTCUSDT', 'ETHUSDT', 'SOLUSDT'])
    expect(res).not.toContain('XAUUSDT') // low_data
    expect(res).not.toContain('TSLAUSDT') // не откалибровано
    expect(usableP(P(0.7, { low_data: true }))).toBeNull()
  })

  it('min EV', () => {
    expect(filterRows(rows, F({ minEv: 0.42 })).map((r) => r.hh_symbol)).toEqual(['ETHUSDT', 'XAUUSDT'])
  })

  it('P и EV должны выполняться на одной стороне', () => {
    // у строки высокий P лонга и высокий EV шорта — не проходит
    const mixed = row('MIXUSDT', { p_long: P(0.65), ev_long_r: 0.0, p_short: P(0.3), ev_short_r: 0.9 })
    expect(rowMatches(mixed, F({ minP: 60, minEv: 0.5 }))).toBe(false)
    expect(rowMatches(mixed, F({ minP: 60, minEv: 0 }))).toBe(true)
  })

  it('только лонг/шорт: active_signal_side или больший score', () => {
    expect(filterRows(rows, F({ side: 'short' })).map((r) => r.hh_symbol)).toEqual(['ETHUSDT'])
    expect(filterRows(rows, F({ side: 'long' })).map((r) => r.hh_symbol)).toEqual(['BTCUSDT', 'XAUUSDT', 'TSLAUSDT'])
    // равный score — ни лонг, ни шорт
    expect(dominantSide(rows[4]!)).toBeNull()
    // сторона сигнала важнее score
    expect(dominantSide(row('X', { active_signal_id: 9, active_signal_side: 'short', score_long: 90, score_short: 10 }))).toBe('short')
  })

  it('сторона + min P — порог по выбранной стороне', () => {
    expect(filterRows(rows, F({ side: 'long', minP: 55 })).map((r) => r.hh_symbol)).toEqual(['BTCUSDT'])
  })

  it('поиск по символу', () => {
    expect(filterRows(rows, F({ search: 'sol' })).map((r) => r.hh_symbol)).toEqual(['SOLUSDT'])
  })
})

describe('санитайзинг и пресеты', () => {
  it('битые данные из localStorage → значения по умолчанию', () => {
    expect(sanitizeFilters(null)).toEqual(DEFAULT_FILTERS)
    expect(sanitizeFilters('x')).toEqual(DEFAULT_FILTERS)
    const f = sanitizeFilters({ classes: ['crypto', 'nope'], onlySignals: 'yes', minP: 250, minEv: 'a', side: 'up', search: 5 })
    expect(f).toEqual({ ...DEFAULT_FILTERS, classes: ['crypto'], minP: 100 })
  })

  it('upsertPreset заменяет пресет с тем же именем', () => {
    let list = upsertPreset([], 'Сигналы', F({ onlySignals: true }))
    list = upsertPreset(list, 'Металлы', F({ classes: ['metal'] }))
    list = upsertPreset(list, 'Сигналы', F({ onlySignals: true, minP: 55 }))
    expect(list.map((p) => p.name)).toEqual(['Металлы', 'Сигналы'])
    expect(list[1]!.filters.minP).toBe(55)
    expect(upsertPreset(list, '   ', F())).toBe(list)
  })

  it('sanitizePresets отбрасывает мусор', () => {
    expect(sanitizePresets([{ name: 'ok', filters: { side: 'long' } }, { name: '' }, 5, null])).toEqual([
      { name: 'ok', filters: { ...DEFAULT_FILTERS, side: 'long' } },
    ])
    expect(sanitizePresets({})).toEqual([])
  })
})
