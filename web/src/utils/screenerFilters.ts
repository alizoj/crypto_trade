// Фильтрация строк скринера — чистые функции (покрыты unit-тестами).
import type { AssetClass, ProbInfo, ScreenerRow, Side } from '@/api/types'

export type SideFilter = 'both' | 'long' | 'short'

export interface ScreenerFilters {
  /** пусто — все классы */
  classes: AssetClass[]
  onlySignals: boolean
  /** минимальная P, % (0..100); null — без фильтра */
  minP: number | null
  /** минимальный EV, R; null — без фильтра */
  minEv: number | null
  side: SideFilter
  search: string
}

export const DEFAULT_FILTERS: ScreenerFilters = {
  classes: [],
  onlySignals: false,
  minP: null,
  minEv: null,
  side: 'both',
  search: '',
}

export function cloneFilters(f: ScreenerFilters): ScreenerFilters {
  return { ...f, classes: [...f.classes] }
}

const ASSET_CLASSES: AssetClass[] = ['crypto', 'equity', 'etf', 'metal', 'energy']

/** Проверка и нормализация фильтров из localStorage (могут быть битые/старые). */
export function sanitizeFilters(raw: unknown): ScreenerFilters {
  const f = cloneFilters(DEFAULT_FILTERS)
  if (!raw || typeof raw !== 'object') return f
  const r = raw as Record<string, unknown>
  if (Array.isArray(r.classes)) f.classes = r.classes.filter((c): c is AssetClass => ASSET_CLASSES.includes(c as AssetClass))
  f.onlySignals = r.onlySignals === true
  f.minP = typeof r.minP === 'number' && Number.isFinite(r.minP) ? Math.min(100, Math.max(0, r.minP)) : null
  f.minEv = typeof r.minEv === 'number' && Number.isFinite(r.minEv) ? r.minEv : null
  f.side = r.side === 'long' || r.side === 'short' ? r.side : 'both'
  f.search = typeof r.search === 'string' ? r.search.slice(0, 32) : ''
  return f
}

export function isDefaultFilters(f: ScreenerFilters): boolean {
  return (
    f.classes.length === 0 && !f.onlySignals && f.minP === null && f.minEv === null && f.side === 'both' && f.search.trim() === ''
  )
}

/** Вероятность, пригодная для сравнения с порогом: откалибрована и данных достаточно. */
export function usableP(prob: ProbInfo | null | undefined): number | null {
  if (!prob || !prob.calibrated || prob.low_data) return null
  return prob.p
}

export function sideProb(row: ScreenerRow, side: Side): ProbInfo | null {
  return side === 'long' ? row.p_long : row.p_short
}

export function sideEv(row: ScreenerRow, side: Side): number | null {
  return side === 'long' ? row.ev_long_r : row.ev_short_r
}

export function sideScore(row: ScreenerRow, side: Side): number | null {
  return side === 'long' ? row.score_long : row.score_short
}

/**
 * Преобладающая сторона строки: сторона активного сигнала (active_signal_side),
 * иначе сторона с большим score. При равенстве/отсутствии — null.
 */
export function dominantSide(row: ScreenerRow): Side | null {
  if (row.active_signal_side) return row.active_signal_side
  const l = row.score_long
  const s = row.score_short
  if (l === null && s === null) return null
  if (s === null || (l !== null && l > s)) return 'long'
  if (l === null || s > l) return 'short'
  return null
}

export function rowMatches(row: ScreenerRow, f: ScreenerFilters): boolean {
  if (f.classes.length > 0 && !f.classes.includes(row.asset_class)) return false
  const q = f.search.trim().toUpperCase()
  if (q && !row.hh_symbol.toUpperCase().includes(q) && !row.base.toUpperCase().includes(q)) return false
  if (f.onlySignals && row.status !== 'signal') return false

  if (f.side !== 'both' && dominantSide(row) !== f.side) return false

  if (f.minP === null && f.minEv === null) return true
  const sides: Side[] = f.side === 'both' ? ['long', 'short'] : [f.side]
  // Порог P и EV должен выполняться на одной и той же стороне.
  return sides.some((side) => {
    if (f.minP !== null) {
      const p = usableP(sideProb(row, side))
      if (p === null || p * 100 < f.minP) return false
    }
    if (f.minEv !== null) {
      const ev = sideEv(row, side)
      if (ev === null || ev < f.minEv) return false
    }
    return true
  })
}

export function filterRows(rows: ScreenerRow[], f: ScreenerFilters): ScreenerRow[] {
  return rows.filter((r) => rowMatches(r, f))
}

// ---------- пресеты ----------

export interface FilterPreset {
  name: string
  filters: ScreenerFilters
}

export function sanitizePresets(raw: unknown): FilterPreset[] {
  if (!Array.isArray(raw)) return []
  const out: FilterPreset[] = []
  for (const item of raw) {
    if (!item || typeof item !== 'object') continue
    const it = item as Record<string, unknown>
    if (typeof it.name !== 'string' || !it.name.trim()) continue
    out.push({ name: it.name.trim().slice(0, 40), filters: sanitizeFilters(it.filters) })
  }
  return out.slice(0, 30)
}

/** Добавляет или заменяет пресет по имени. */
export function upsertPreset(list: FilterPreset[], name: string, filters: ScreenerFilters): FilterPreset[] {
  const n = name.trim().slice(0, 40)
  if (!n) return list
  const next = list.filter((p) => p.name !== n)
  next.push({ name: n, filters: cloneFilters(filters) })
  return next
}
