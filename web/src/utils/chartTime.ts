// Форматтеры времени для Lightweight Charts с учётом переключателя UTC/локальное.
import type { TzMode } from './format'

/** Тип деления шкалы времени (совпадает с TickMarkType из lightweight-charts). */
export const TickKind = { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 } as const

const MONTHS_RU = ['янв', 'фев', 'мар', 'апр', 'мая', 'июн', 'июл', 'авг', 'сен', 'окт', 'ноя', 'дек']

interface Parts {
  y: number
  mo: number
  d: number
  h: number
  mi: number
  s: number
}

function parts(unixSec: number, tz: TzMode): Parts {
  const d = new Date(unixSec * 1000)
  if (tz === 'utc') {
    return { y: d.getUTCFullYear(), mo: d.getUTCMonth(), d: d.getUTCDate(), h: d.getUTCHours(), mi: d.getUTCMinutes(), s: d.getUTCSeconds() }
  }
  return { y: d.getFullYear(), mo: d.getMonth(), d: d.getDate(), h: d.getHours(), mi: d.getMinutes(), s: d.getSeconds() }
}

const p2 = (n: number) => String(n).padStart(2, '0')

/** Подпись деления шкалы времени. */
export function tickLabel(unixSec: number, kind: number, tz: TzMode): string {
  const p = parts(unixSec, tz)
  switch (kind) {
    case TickKind.Year:
      return String(p.y)
    case TickKind.Month:
      return `${MONTHS_RU[p.mo]} ${p.y}`
    case TickKind.DayOfMonth:
      return `${p.d} ${MONTHS_RU[p.mo]}`
    case TickKind.TimeWithSeconds:
      return `${p2(p.h)}:${p2(p.mi)}:${p2(p.s)}`
    default:
      return `${p2(p.h)}:${p2(p.mi)}`
  }
}

/** Полная метка времени для перекрестия: «23.09.26 14:00». */
export function crosshairLabel(unixSec: number, tz: TzMode): string {
  const p = parts(unixSec, tz)
  return `${p2(p.d)}.${p2(p.mo + 1)}.${String(p.y).slice(2)} ${p2(p.h)}:${p2(p.mi)}${tz === 'utc' ? ' UTC' : ''}`
}
