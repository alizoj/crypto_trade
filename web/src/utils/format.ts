// Форматирование чисел и времени. Десятичный разделитель — точка (цены копируются
// в терминал), разделитель тысяч — неразрывный пробел. Отсутствующее значение — «—».

export const DASH = '—'
const NBSP = ' '

export type TzMode = 'utc' | 'local'

function isNum(v: number | null | undefined): v is number {
  return typeof v === 'number' && Number.isFinite(v)
}

/** toFixed без «-0.00». */
export function toFixedClean(v: number, decimals: number): string {
  const s = v.toFixed(decimals)
  return /^-0(\.0+)?$/.test(s) ? s.slice(1) : s
}

function group(intPart: string): string {
  return intPart.replace(/\B(?=(\d{3})+(?!\d))/g, NBSP)
}

/** Число с фиксированным числом знаков. */
export function fmtNum(v: number | null | undefined, decimals = 2, opts: { group?: boolean; sign?: boolean } = {}): string {
  if (!isNum(v)) return DASH
  let s = toFixedClean(Math.abs(v), decimals)
  if (opts.group) {
    const [i, f] = s.split('.')
    s = f !== undefined ? `${group(i!)}.${f}` : group(i!)
  }
  const negative = v < 0 && Number(toFixedClean(v, decimals)) !== 0
  if (negative) return `-${s}`
  if (opts.sign && Number(s.replace(/ /g, '')) !== 0) return `+${s}`
  return s
}

/** Число знаков после точки для цены по её величине (tick size в контракте нет). */
export function priceDecimals(v: number): number {
  const a = Math.abs(v)
  if (a === 0) return 2
  if (a >= 10000) return 1
  if (a >= 1000) return 2
  if (a >= 10) return 3
  if (a >= 1) return 4
  if (a >= 0.1) return 5
  if (a >= 0.01) return 6
  return Math.min(10, Math.ceil(-Math.log10(a)) + 3)
}

export function fmtPrice(v: number | null | undefined, decimals?: number): string {
  if (!isNum(v)) return DASH
  return fmtNum(v, decimals ?? priceDecimals(v))
}

/** Значение, уже выраженное в процентах (1.23 → «1.23%»). */
export function fmtPct(v: number | null | undefined, decimals = 2, sign = false): string {
  if (!isNum(v)) return DASH
  return `${fmtNum(v, decimals, { sign })}%`
}

/** Доля → проценты (0.613 → «61%»). */
export function fmtShare(v: number | null | undefined, decimals = 0): string {
  if (!isNum(v)) return DASH
  return `${fmtNum(v * 100, decimals)}%`
}

/** Funding rate: доля (0.0001 = 0.01%) → «0.0100%». */
export function fmtFunding(rate: number | null | undefined): string {
  if (!isNum(rate)) return DASH
  return `${fmtNum(rate * 100, 4)}%`
}

/** EV / результат в R: «+0.38R». */
export function fmtR(v: number | null | undefined, decimals = 2): string {
  if (!isNum(v)) return DASH
  return `${fmtNum(v, decimals, { sign: true })}R`
}

/** Доллары: «$12 500», «-$1 250.50». */
export function fmtUsd(v: number | null | undefined, decimals = 0, sign = false): string {
  if (!isNum(v)) return DASH
  const abs = fmtNum(Math.abs(v), decimals, { group: true })
  const zero = Number(toFixedClean(v, decimals)) === 0
  if (v < 0 && !zero) return `-$${abs}`
  if (sign && !zero) return `+$${abs}`
  return `$${abs}`
}

/** Компактные доллары для лимитов: 250000 → «$250K», 1500000 → «$1.5M». */
export function fmtUsdCompact(v: number | null | undefined): string {
  if (!isNum(v)) return DASH
  const a = Math.abs(v)
  const sgn = v < 0 ? '-' : ''
  if (a >= 1_000_000) return `${sgn}$${trimZeros((a / 1_000_000).toFixed(2))}M`
  if (a >= 1_000) return `${sgn}$${trimZeros((a / 1_000).toFixed(1))}K`
  return `${sgn}$${trimZeros(a.toFixed(0))}`
}

function trimZeros(s: string): string {
  return s.includes('.') ? s.replace(/\.?0+$/, '') : s
}

export function fmtQty(v: number | null | undefined): string {
  if (!isNum(v)) return DASH
  const a = Math.abs(v)
  const d = a >= 1000 ? 0 : a >= 1 ? 3 : 6
  return trimZeros(fmtNum(v, d))
}

/** Интервал Wilson в процентах: «55–67%». */
export function fmtCi(low: number | null | undefined, high: number | null | undefined): string {
  if (!isNum(low) || !isNum(high)) return DASH
  return `${fmtNum(low * 100, 0)}–${fmtNum(high * 100, 0)}%`
}

// ---------------- время ----------------

const dtfCache = new Map<string, Intl.DateTimeFormat>()

function dtf(tz: TzMode, withDate: boolean, withSeconds: boolean, withYear: boolean): Intl.DateTimeFormat {
  const key = `${tz}|${withDate}|${withSeconds}|${withYear}`
  let f = dtfCache.get(key)
  if (!f) {
    f = new Intl.DateTimeFormat('ru-RU', {
      timeZone: tz === 'utc' ? 'UTC' : undefined,
      year: withDate && withYear ? 'numeric' : undefined,
      month: withDate ? '2-digit' : undefined,
      day: withDate ? '2-digit' : undefined,
      hour: '2-digit',
      minute: '2-digit',
      second: withSeconds ? '2-digit' : undefined,
      hourCycle: 'h23',
    })
    dtfCache.set(key, f)
  }
  return f
}

export function toDate(t: string | number | Date): Date {
  if (t instanceof Date) return t
  if (typeof t === 'number') return new Date(t * 1000) // unix-секунды
  return new Date(t)
}

function assemble(parts: Intl.DateTimeFormatPart[]): { date: string; time: string } {
  const get = (type: Intl.DateTimeFormatPartTypes) => parts.find((p) => p.type === type)?.value
  const day = get('day')
  const month = get('month')
  const year = get('year')
  const time = [get('hour'), get('minute'), get('second')].filter(Boolean).join(':')
  const date = day && month ? [day, month, year].filter(Boolean).join('.') : ''
  return { date, time }
}

export interface DateFmtOptions {
  date?: boolean
  seconds?: boolean
  year?: boolean
}

/** «23.09.2026 12:00» в выбранном поясе. Принимает ISO-строку, unix-секунды или Date. */
export function fmtDateTime(t: string | number | Date | null | undefined, tz: TzMode, opts: DateFmtOptions = {}): string {
  if (t === null || t === undefined || t === '') return DASH
  const d = toDate(t)
  if (Number.isNaN(d.getTime())) return DASH
  const withDate = opts.date ?? true
  const { date, time } = assemble(dtf(tz, withDate, opts.seconds ?? false, opts.year ?? true).formatToParts(d))
  return withDate ? `${date} ${time}` : time
}

export function fmtTime(t: string | number | Date | null | undefined, tz: TzMode, seconds = false): string {
  return fmtDateTime(t, tz, { date: false, seconds })
}

/** Короткая подпись пояса: «UTC» или «UTC+4». */
export function tzLabel(tz: TzMode, at: Date = new Date()): string {
  if (tz === 'utc') return 'UTC'
  const off = -at.getTimezoneOffset()
  if (off === 0) return 'UTC±0'
  const sign = off > 0 ? '+' : '-'
  const h = Math.floor(Math.abs(off) / 60)
  const m = Math.abs(off) % 60
  return `UTC${sign}${h}${m ? `:${String(m).padStart(2, '0')}` : ''}`
}

/** Длительность: «5ч 12м», «12м 05с», «2д 3ч». */
export function fmtDuration(ms: number | null | undefined): string {
  if (!isNum(ms)) return DASH
  const total = Math.max(0, Math.floor(ms / 1000))
  const d = Math.floor(total / 86400)
  const h = Math.floor((total % 86400) / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = total % 60
  if (d > 0) return `${d}д ${h}ч`
  if (h > 0) return `${h}ч ${String(m).padStart(2, '0')}м`
  return `${m}м ${String(s).padStart(2, '0')}с`
}

/** Возраст данных в секундах: «12 с», «3 мин», «2 ч». */
export function fmtAge(sec: number | null | undefined): string {
  if (!isNum(sec)) return DASH
  if (sec < 60) return `${Math.round(sec)} с`
  if (sec < 3600) return `${Math.round(sec / 60)} мин`
  if (sec < 86400) return `${Math.round(sec / 3600)} ч`
  return `${Math.round(sec / 86400)} д`
}

/** Разбор числа из текста UI (для тестов и полей ввода): «-1 234.5%» → -1234.5. */
export function parseNum(text: string): number | null {
  const cleaned = text.replace(/[\s  $%R+]/g, '').replace(/,/g, '.').replace(/−/g, '-')
  if (!cleaned || cleaned === DASH) return null
  const n = Number(cleaned)
  return Number.isFinite(n) ? n : null
}
