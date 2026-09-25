// Описание колонок скринера (раздел 8.3): заголовки, подсказки, значения для сортировки.
import type { ScreenerRow, ScreenerStatus, Side } from '@/api/types'
import { usableP } from '@/utils/screenerFilters'
import { ASSET_CLASS_RU } from '@/utils/labels'

export type ColId =
  | 'symbol'
  | 'class'
  | 'price'
  | 'change'
  | 'htf'
  | 'rsi'
  | 'adx'
  | 'vwap'
  | 'atr'
  | 'funding'
  | 'oi'
  | 'cvd'
  | 'score_long'
  | 'score_short'
  | 'p_long'
  | 'p_short'
  | 'ev_long'
  | 'ev_short'
  | 'status'
  | 'limit'

export interface ScreenerColumn {
  id: ColId
  header: string
  title: string
  /** сторона — колонка скрывается фильтром «только лонг/шорт» */
  side?: Side
  text?: boolean
  align?: 'left' | 'center' | 'right'
  sortValue: (r: ScreenerRow) => number | string | undefined
}

const n = (v: number | null | undefined): number | undefined => (typeof v === 'number' && Number.isFinite(v) ? v : undefined)

export const STATUS_ORDER: Record<ScreenerStatus, number> = { signal: 0, watch: 1, none: 2, off_session: 3, halted: 4 }

export const SCREENER_COLUMNS: ScreenerColumn[] = [
  { id: 'symbol', header: 'Символ', title: 'Тикер HashHedge', text: true, align: 'left', sortValue: (r) => r.hh_symbol },
  { id: 'class', header: 'Класс', title: 'Класс актива', text: true, align: 'left', sortValue: (r) => ASSET_CLASS_RU[r.asset_class] },
  { id: 'price', header: 'Цена', title: 'Последняя цена', sortValue: (r) => n(r.price) },
  { id: 'change', header: '24h', title: 'Изменение за 24 часа, %', sortValue: (r) => n(r.change_24h_pct) },
  { id: 'htf', header: 'HTF', title: 'Тренд старшего таймфрейма', align: 'center', sortValue: (r) => r.htf_trend },
  { id: 'rsi', header: 'RSI', title: 'RSI(14)', sortValue: (r) => n(r.rsi14) },
  { id: 'adx', header: 'ADX', title: 'ADX(14) — сила тренда', sortValue: (r) => n(r.adx) },
  { id: 'vwap', header: 'VWAP', title: 'Расстояние до VWAP, %', sortValue: (r) => n(r.vwap_dist_pct) },
  { id: 'atr', header: 'ATR%', title: 'ATR(14) в % от цены', sortValue: (r) => n(r.atr_pct) },
  { id: 'funding', header: 'Funding', title: 'Текущая ставка funding', sortValue: (r) => n(r.funding_rate) },
  { id: 'oi', header: 'ΔOI', title: 'Изменение открытого интереса за 24 часа, %', sortValue: (r) => n(r.oi_change_24h_pct) },
  { id: 'cvd', header: 'CVD', title: 'Тренд кумулятивной дельты объёма', align: 'center', sortValue: (r) => r.cvd_trend },
  { id: 'score_long', header: 'Score L', title: 'Score лонга 0–100 (не вероятность)', side: 'long', sortValue: (r) => n(r.score_long) },
  { id: 'score_short', header: 'Score S', title: 'Score шорта 0–100 (не вероятность)', side: 'short', sortValue: (r) => n(r.score_short) },
  {
    id: 'p_long',
    header: 'P(long)',
    title: 'Откалиброванная вероятность TP раньше SL для лонга; ДИ и n — в подсказке',
    side: 'long',
    sortValue: (r) => n(usableP(r.p_long)),
  },
  {
    id: 'p_short',
    header: 'P(short)',
    title: 'Откалиброванная вероятность TP раньше SL для шорта; ДИ и n — в подсказке',
    side: 'short',
    sortValue: (r) => n(usableP(r.p_short)),
  },
  { id: 'ev_long', header: 'EV L', title: 'Ожидание лонга в R: P×RR − (1−P) − комиссии', side: 'long', sortValue: (r) => n(r.ev_long_r) },
  { id: 'ev_short', header: 'EV S', title: 'Ожидание шорта в R: P×RR − (1−P) − комиссии', side: 'short', sortValue: (r) => n(r.ev_short_r) },
  { id: 'status', header: 'Статус', title: 'Сигнал / наблюдение / нет / вне сессии / halted', text: false, align: 'left', sortValue: (r) => STATUS_ORDER[r.status] },
  { id: 'limit', header: 'Лимит', title: 'Лимит HashHedge на одну позицию (max_per_lot), в подсказке — max_total', sortValue: (r) => r.max_per_lot_usd },
]

export const COLUMN_BY_ID = new Map(SCREENER_COLUMNS.map((c) => [c.id, c]))
