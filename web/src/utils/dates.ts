// Преобразование дат из полей <input type="date"> в ISO-моменты UTC с учётом выбранного пояса.
import type { TzMode } from './format'

function isoNoMs(ms: number): string {
  return new Date(ms).toISOString().replace(/\.\d{3}Z$/, 'Z')
}

function startOfDayMs(date: string, tz: TzMode): number | null {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date)
  if (!m) return null
  const [y, mo, d] = [Number(m[1]), Number(m[2]) - 1, Number(m[3])]
  return tz === 'utc' ? Date.UTC(y, mo, d) : new Date(y, mo, d).getTime()
}

/** Начало дня `date` (YYYY-MM-DD) в поясе tz → ISO UTC. */
export function dayStartIso(date: string, tz: TzMode): string | undefined {
  const ms = startOfDayMs(date, tz)
  return ms === null ? undefined : isoNoMs(ms)
}

/** Начало следующего за `date` дня в поясе tz → ISO UTC (граница «to» — исключительно). */
export function dayEndExclusiveIso(date: string, tz: TzMode): string | undefined {
  const m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(date)
  if (!m) return undefined
  const [y, mo, d] = [Number(m[1]), Number(m[2]) - 1, Number(m[3])]
  const next = tz === 'utc' ? Date.UTC(y, mo, d + 1) : new Date(y, mo, d + 1).getTime()
  return isoNoMs(next)
}
