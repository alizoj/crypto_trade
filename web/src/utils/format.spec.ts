import { describe, expect, it } from 'vitest'
import {
  DASH,
  fmtAge,
  fmtCi,
  fmtDateTime,
  fmtDuration,
  fmtFunding,
  fmtNum,
  fmtPct,
  fmtPrice,
  fmtQty,
  fmtR,
  fmtShare,
  fmtTime,
  fmtUsd,
  fmtUsdCompact,
  parseNum,
  priceDecimals,
  toFixedClean,
  tzLabel,
} from './format'

const NB = ' '

describe('числа', () => {
  it('fmtNum: знаки, группировка, отсутствие значения', () => {
    expect(fmtNum(1234.567, 2)).toBe('1234.57')
    expect(fmtNum(1234567.8, 1, { group: true })).toBe(`1${NB}234${NB}567.8`)
    expect(fmtNum(0.5, 2, { sign: true })).toBe('+0.50')
    expect(fmtNum(-0.5, 2, { sign: true })).toBe('-0.50')
    expect(fmtNum(0, 2, { sign: true })).toBe('0.00')
    expect(fmtNum(null)).toBe(DASH)
    expect(fmtNum(undefined)).toBe(DASH)
    expect(fmtNum(Number.NaN)).toBe(DASH)
  })

  it('нет «-0»', () => {
    expect(toFixedClean(-0.0001, 2)).toBe('0.00')
    expect(fmtNum(-0.0001, 2)).toBe('0.00')
    expect(fmtUsd(-0.001, 2)).toBe('$0.00')
  })

  it('priceDecimals по величине цены', () => {
    expect(priceDecimals(112450)).toBe(1)
    expect(priceDecimals(4381.5)).toBe(2)
    expect(priceDecimals(212.43)).toBe(3)
    expect(priceDecimals(2.9412)).toBe(4)
    expect(priceDecimals(0.23814)).toBe(5)
    expect(priceDecimals(0.021432)).toBe(6)
    // PEPE: ~1e-5 → 8 знаков, значимые цифры не теряются
    expect(priceDecimals(0.000010523)).toBe(8)
    expect(fmtPrice(0.000010523)).toBe('0.00001052')
    expect(fmtPrice(112450)).toBe('112450.0')
    expect(fmtPrice(null)).toBe(DASH)
  })

  it('проценты, доли, funding, R', () => {
    expect(fmtPct(1.234, 2)).toBe('1.23%')
    expect(fmtPct(1.234, 2, true)).toBe('+1.23%')
    expect(fmtPct(-7.28, 2, true)).toBe('-7.28%')
    expect(fmtShare(0.613)).toBe('61%')
    expect(fmtShare(0.6135, 1)).toBe('61.4%')
    expect(fmtFunding(0.0001)).toBe('0.0100%')
    expect(fmtFunding(-0.000058)).toBe('-0.0058%')
    expect(fmtR(0.38)).toBe('+0.38R')
    expect(fmtR(-1.06)).toBe('-1.06R')
    expect(fmtCi(0.55, 0.674)).toBe('55–67%')
    expect(fmtCi(null, 0.6)).toBe(DASH)
  })

  it('доллары', () => {
    expect(fmtUsd(12500)).toBe(`$12${NB}500`)
    expect(fmtUsd(-1250.5, 2)).toBe(`-$1${NB}250.50`)
    expect(fmtUsd(15.3, 2, true)).toBe('+$15.30')
    expect(fmtUsdCompact(250000)).toBe('$250K')
    expect(fmtUsdCompact(7500)).toBe('$7.5K')
    expect(fmtUsdCompact(1500000)).toBe('$1.5M')
    expect(fmtUsdCompact(1000)).toBe('$1K')
  })

  it('количество', () => {
    expect(fmtQty(12)).toBe('12')
    expect(fmtQty(0.6)).toBe('0.6')
    expect(fmtQty(0.050828)).toBe('0.050828')
    expect(fmtQty(12345.6)).toBe('12346')
  })

  it('parseNum — обратное преобразование текста UI', () => {
    expect(parseNum(`-$1${NB}250.50`)).toBe(-1250.5)
    expect(parseNum('+0.38R')).toBe(0.38)
    expect(parseNum('61%')).toBe(61)
    expect(parseNum(DASH)).toBeNull()
    expect(parseNum('1,5')).toBe(1.5)
  })
})

describe('время', () => {
  const iso = '2026-09-23T20:05:09Z'

  it('UTC', () => {
    expect(fmtDateTime(iso, 'utc')).toBe('23.09.2026 20:05')
    expect(fmtDateTime(iso, 'utc', { year: false, seconds: true })).toBe('23.09 20:05:09')
    expect(fmtTime(iso, 'utc')).toBe('20:05')
    // unix-секунды (как у свечей)
    expect(fmtDateTime(Date.parse(iso) / 1000, 'utc')).toBe('23.09.2026 20:05')
    expect(fmtDateTime(null, 'utc')).toBe(DASH)
    expect(fmtDateTime('мусор', 'utc')).toBe(DASH)
  })

  it('локальный пояс совпадает с Date в локальном поясе процесса', () => {
    const d = new Date(iso)
    const hh = String(d.getHours()).padStart(2, '0')
    const mm = String(d.getMinutes()).padStart(2, '0')
    expect(fmtTime(iso, 'local')).toBe(`${hh}:${mm}`)
    expect(tzLabel('utc')).toBe('UTC')
    expect(tzLabel('local')).toMatch(/^UTC[+-±]/)
  })

  it('длительности и возраст данных', () => {
    expect(fmtDuration((5 * 3600 + 12 * 60) * 1000)).toBe('5ч 12м')
    expect(fmtDuration((12 * 60 + 5) * 1000)).toBe('12м 05с')
    expect(fmtDuration((2 * 86400 + 3 * 3600) * 1000)).toBe('2д 3ч')
    expect(fmtDuration(-5)).toBe('0м 00с')
    expect(fmtAge(12)).toBe('12 с')
    expect(fmtAge(180)).toBe('3 мин')
    expect(fmtAge(5400)).toBe('2 ч')
  })
})
