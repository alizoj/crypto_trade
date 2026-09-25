// Цветовая индикация вероятностей (раздел 8.3 ТЗ):
// зелёный/красный по стороне, насыщенность по P; серый — «мало данных»,
// «не откалибровано», «вне сессии»/halted, устаревшая калибровка.
import type { ProbInfo, ScreenerStatus, Side } from '@/api/types'
import { DASH, fmtCi, fmtNum } from './format'

export type ProbKind = 'long' | 'short' | 'gray'

export type GrayReason = 'no_data' | 'low_data' | 'uncalibrated' | 'off_session' | 'halted' | 'stale' | null

export interface ProbView {
  kind: ProbKind
  /** 0..1 — непрозрачность заливки (насыщенность по P); для серого фиксированная */
  alpha: number
  /** основной текст ячейки */
  label: string
  /** подпись под значением / в карточке */
  sublabel: string | null
  /** текст тултипа: ДИ, n, пометки */
  tooltip: string
  grayReason: GrayReason
  /** числовое значение для сортировки/фильтров (null, если вероятность не показывается) */
  value: number | null
}

/** Диапазон P, на котором насыщенность растёт от минимума до максимума. */
export const P_SAT_LOW = 0.4
export const P_SAT_HIGH = 0.75
export const ALPHA_MIN = 0.1
export const ALPHA_MAX = 0.6
export const GRAY_ALPHA = 0.14

/** Насыщенность (непрозрачность заливки) по P, монотонно неубывающая. */
export function probAlpha(p: number): number {
  if (!Number.isFinite(p)) return ALPHA_MIN
  const t = Math.min(1, Math.max(0, (p - P_SAT_LOW) / (P_SAT_HIGH - P_SAT_LOW)))
  return Math.round((ALPHA_MIN + t * (ALPHA_MAX - ALPHA_MIN)) * 1000) / 1000
}

export interface ProbContext {
  status?: ScreenerStatus
  /** калибровка live помечена устаревшей (алерт «калибровка устарела») */
  stale?: boolean
  /** score стороны — показывается, если вероятность не откалибрована */
  score?: number | null
}

const SIDE_RU: Record<Side, string> = { long: 'лонг', short: 'шорт' }

export function probView(prob: ProbInfo | null | undefined, side: Side, ctx: ProbContext = {}): ProbView {
  const scoreTxt = ctx.score === null || ctx.score === undefined ? DASH : fmtNum(ctx.score, 0)
  if (!prob) {
    return {
      kind: 'gray',
      alpha: 0,
      label: DASH,
      sublabel: null,
      tooltip: `P(${SIDE_RU[side]}): нет данных`,
      grayReason: 'no_data',
      value: null,
    }
  }
  const ci = fmtCi(prob.ci_low, prob.ci_high)
  const base = `P(${SIDE_RU[side]}) = ${fmtNum(prob.p * 100, 1)}%, ДИ 95% ${ci}, n = ${prob.n}`

  if (!prob.calibrated) {
    return {
      kind: 'gray',
      alpha: GRAY_ALPHA,
      label: scoreTxt,
      sublabel: 'не откалибровано',
      tooltip: `Не откалибровано: показан score ${scoreTxt}, это не вероятность. n = ${prob.n}`,
      grayReason: 'uncalibrated',
      value: null,
    }
  }
  if (prob.low_data) {
    return {
      kind: 'gray',
      alpha: GRAY_ALPHA,
      label: 'мало данных',
      sublabel: `n = ${prob.n}`,
      tooltip: `Мало данных: n = ${prob.n} — меньше минимума для оценки. ${base}`,
      grayReason: 'low_data',
      value: null,
    }
  }

  const pctLabel = `${fmtNum(prob.p * 100, 0)}%`
  const grayReason: GrayReason =
    ctx.status === 'halted' ? 'halted' : ctx.status === 'off_session' ? 'off_session' : ctx.stale ? 'stale' : null
  if (grayReason) {
    const note =
      grayReason === 'halted' ? 'торги приостановлены (halted)' : grayReason === 'off_session' ? 'вне сессии базового рынка' : 'калибровка устарела'
    return {
      kind: 'gray',
      alpha: GRAY_ALPHA,
      label: pctLabel,
      sublabel: ci,
      tooltip: `${base}. Серым: ${note}`,
      grayReason,
      value: prob.p,
    }
  }
  return {
    kind: side,
    alpha: probAlpha(prob.p),
    label: pctLabel,
    sublabel: ci,
    tooltip: base,
    grayReason: null,
    value: prob.p,
  }
}

/** CSS background для ячейки вероятности (цвета — CSS-переменные темы). */
export function probBackground(v: Pick<ProbView, 'kind' | 'alpha'>): string {
  if (v.alpha <= 0) return 'transparent'
  const varName = v.kind === 'long' ? '--long-rgb' : v.kind === 'short' ? '--short-rgb' : '--gray-rgb'
  return `rgb(var(${varName}) / ${v.alpha})`
}

/** Класс цвета текста для знакового числа. */
export function signClass(v: number | null | undefined): string {
  if (typeof v !== 'number' || !Number.isFinite(v) || v === 0) return 'text-muted'
  return v > 0 ? 'text-long' : 'text-short'
}

export function trendClass(t: -1 | 0 | 1 | null | undefined): string {
  if (t === 1) return 'text-long'
  if (t === -1) return 'text-short'
  return 'text-muted'
}

export function trendArrow(t: -1 | 0 | 1 | null | undefined): string {
  if (t === 1) return '▲'
  if (t === -1) return '▼'
  if (t === 0) return '▬'
  return DASH
}

export function trendLabel(t: -1 | 0 | 1 | null | undefined): string {
  if (t === 1) return 'вверх'
  if (t === -1) return 'вниз'
  if (t === 0) return 'флэт'
  return 'нет данных'
}

/** RSI: перекупленность/перепроданность. */
export function rsiClass(v: number | null | undefined): string {
  if (typeof v !== 'number') return 'text-muted'
  if (v >= 70) return 'text-short'
  if (v <= 30) return 'text-long'
  return ''
}
