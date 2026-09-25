import { describe, expect, it } from 'vitest'
import type { ProbInfo } from '@/api/types'
import { ALPHA_MAX, ALPHA_MIN, GRAY_ALPHA, probAlpha, probBackground, probView, signClass, trendArrow, trendClass } from './colors'

const prob = (p: number, over: Partial<ProbInfo> = {}): ProbInfo => ({
  p,
  ci_low: p - 0.05,
  ci_high: p + 0.05,
  n: 412,
  calibrated: true,
  low_data: false,
  ...over,
})

describe('probAlpha — насыщенность по P', () => {
  it('монотонно не убывает и ограничена', () => {
    let prev = -1
    for (let p = 0; p <= 1.0001; p += 0.01) {
      const a = probAlpha(p)
      expect(a).toBeGreaterThanOrEqual(prev)
      expect(a).toBeGreaterThanOrEqual(ALPHA_MIN)
      expect(a).toBeLessThanOrEqual(ALPHA_MAX)
      prev = a
    }
    expect(probAlpha(0.2)).toBe(ALPHA_MIN)
    expect(probAlpha(0.9)).toBe(ALPHA_MAX)
    expect(probAlpha(0.65)).toBeGreaterThan(probAlpha(0.55))
  })
})

describe('probView — цвет и подпись вероятности', () => {
  it('откалиброванная P: цвет по стороне, процент, ДИ и n в тултипе', () => {
    const v = probView(prob(0.61, { ci_low: 0.55, ci_high: 0.67 }), 'long')
    expect(v.kind).toBe('long')
    expect(v.label).toBe('61%')
    expect(v.sublabel).toBe('55–67%')
    expect(v.tooltip).toContain('ДИ 95% 55–67%')
    expect(v.tooltip).toContain('n = 412')
    expect(v.grayReason).toBeNull()
    expect(v.value).toBe(0.61)
    expect(probView(prob(0.58), 'short').kind).toBe('short')
  })

  it('low_data → серый «мало данных» вместо процента', () => {
    const v = probView(prob(0.7, { low_data: true, n: 42 }), 'long')
    expect(v.kind).toBe('gray')
    expect(v.label).toBe('мало данных')
    expect(v.label).not.toContain('%')
    expect(v.grayReason).toBe('low_data')
    expect(v.value).toBeNull()
    expect(v.alpha).toBe(GRAY_ALPHA)
  })

  it('calibrated=false → серый, показывается score с пометкой', () => {
    const v = probView(prob(0.66, { calibrated: false }), 'short', { score: 72 })
    expect(v.kind).toBe('gray')
    expect(v.label).toBe('72')
    expect(v.sublabel).toBe('не откалибровано')
    expect(v.grayReason).toBe('uncalibrated')
    expect(v.value).toBeNull()
  })

  it('не откалибровано приоритетнее «мало данных»', () => {
    expect(probView(prob(0.5, { calibrated: false, low_data: true }), 'long', { score: 40 }).grayReason).toBe('uncalibrated')
  })

  it('off_session / halted / устаревшая калибровка → серый, процент остаётся', () => {
    for (const [ctx, reason] of [
      [{ status: 'off_session' as const }, 'off_session'],
      [{ status: 'halted' as const }, 'halted'],
      [{ stale: true }, 'stale'],
    ] as const) {
      const v = probView(prob(0.63), 'long', ctx)
      expect(v.kind).toBe('gray')
      expect(v.label).toBe('63%')
      expect(v.grayReason).toBe(reason)
      expect(v.value).toBe(0.63)
    }
  })

  it('нет вероятности → прочерк', () => {
    const v = probView(null, 'long')
    expect(v.label).toBe('—')
    expect(v.grayReason).toBe('no_data')
    expect(probBackground(v)).toBe('transparent')
  })

  it('probBackground ссылается на CSS-переменные темы', () => {
    expect(probBackground({ kind: 'long', alpha: 0.3 })).toBe('rgb(var(--long-rgb) / 0.3)')
    expect(probBackground({ kind: 'short', alpha: 0.5 })).toBe('rgb(var(--short-rgb) / 0.5)')
    expect(probBackground({ kind: 'gray', alpha: 0.14 })).toBe('rgb(var(--gray-rgb) / 0.14)')
  })
})

describe('знаки и тренды', () => {
  it('signClass', () => {
    expect(signClass(1)).toBe('text-long')
    expect(signClass(-0.1)).toBe('text-short')
    expect(signClass(0)).toBe('text-muted')
    expect(signClass(null)).toBe('text-muted')
  })
  it('trendArrow/trendClass', () => {
    expect(trendArrow(1)).toBe('▲')
    expect(trendArrow(-1)).toBe('▼')
    expect(trendArrow(0)).toBe('▬')
    expect(trendClass(1)).toBe('text-long')
    expect(trendClass(-1)).toBe('text-short')
  })
})
