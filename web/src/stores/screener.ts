// Скринер: строки текущего ТФ, live-обновления через WS, фильтры и пресеты.
import { defineStore } from 'pinia'
import { computed, ref, shallowRef, watch } from 'vue'
import { api } from '@/api/endpoints'
import { errorMessage, notifyUnauthorized } from '@/api/client'
import { ScreenerSocket, type WsState } from '@/api/ws'
import type { ScreenerRow, Signal, Timeframe, WsServerMessage } from '@/api/types'
import {
  DEFAULT_FILTERS,
  cloneFilters,
  filterRows,
  sanitizeFilters,
  sanitizePresets,
  upsertPreset,
  type FilterPreset,
  type ScreenerFilters,
} from '@/utils/screenerFilters'
import { STORAGE_KEYS, readJSON, writeJSON } from '@/utils/storage'
import { useAccountsStore } from './accounts'
import { useAuthStore } from './auth'
import { useMetaStore } from './meta'

const TFS: Timeframe[] = ['15m', '1h', '4h']

export interface RowsState {
  tf: Timeframe
  rows: ScreenerRow[]
  updatedAt: string | null
  /** получен snapshot для текущего ТФ */
  ready: boolean
}

/**
 * Применяет сообщение snapshot/update к состоянию (чистая функция).
 * Сообщения для другого ТФ игнорируются. Возвращает новое состояние или null, если ничего не изменилось.
 */
export function applyRowsMessage(state: RowsState, msg: WsServerMessage): RowsState | null {
  if (msg.type !== 'snapshot' && msg.type !== 'update') return null
  if (msg.tf !== state.tf) return null
  if (msg.type === 'snapshot') {
    return { tf: state.tf, rows: msg.rows.slice(), updatedAt: msg.updated_at, ready: true }
  }
  if (!msg.rows.length) return { ...state, updatedAt: msg.updated_at }
  const bySym = new Map(msg.rows.map((r) => [r.hh_symbol, r]))
  const rows = state.rows.map((r) => {
    const u = bySym.get(r.hh_symbol)
    if (u) bySym.delete(r.hh_symbol)
    return u ?? r
  })
  // новые символы (например, добавленные в конфиг) — в конец
  for (const r of bySym.values()) rows.push(r)
  return { tf: state.tf, rows, updatedAt: msg.updated_at, ready: state.ready }
}

interface PersistedState {
  tf?: string
  filters?: unknown
}

function readPersisted(): { tf: Timeframe; filters: ScreenerFilters } {
  const raw = readJSON<PersistedState>(STORAGE_KEYS.screenerState, {})
  const tf = TFS.includes(raw.tf as Timeframe) ? (raw.tf as Timeframe) : '1h'
  return { tf, filters: sanitizeFilters(raw.filters) }
}

export const useScreenerStore = defineStore('screener', () => {
  const accounts = useAccountsStore()
  const auth = useAuthStore()
  const meta = useMetaStore()
  const persisted = readPersisted()

  const state = shallowRef<RowsState>({ tf: persisted.tf, rows: [], updatedAt: null, ready: false })
  const tf = computed(() => state.value.tf)
  const wsState = ref<WsState>('closed')
  const wsAttempt = ref(0)
  const wsNextRetryMs = ref<number | null>(null)
  const lastMessageAt = ref<number | null>(null)
  const error = ref<string | null>(null)
  const filters = ref<ScreenerFilters>(persisted.filters)
  const presets = ref<FilterPreset[]>(sanitizePresets(readJSON(STORAGE_KEYS.screenerPresets, [])))
  /** последнее WS-сообщение {type:"signal"} — журнал обновляет первую страницу */
  const lastSignal = shallowRef<Signal | null>(null)
  /** глобальный флаг /api/meta.calibration_stale — вероятности серым */
  const calibrationStale = computed(() => meta.meta?.calibration_stale === true)

  let socket: ScreenerSocket | null = null
  let restFallbackTimer: ReturnType<typeof setTimeout> | null = null

  watch(
    [filters, tf],
    () => writeJSON(STORAGE_KEYS.screenerState, { tf: tf.value, filters: filters.value }),
    { deep: true },
  )
  watch(presets, (p) => writeJSON(STORAGE_KEYS.screenerPresets, p), { deep: true })

  const filteredRows = computed(() => filterRows(state.value.rows, filters.value))

  function handleMessage(msg: WsServerMessage): void {
    lastMessageAt.value = Date.now()
    switch (msg.type) {
      case 'snapshot':
      case 'update': {
        const next = applyRowsMessage(state.value, msg)
        if (next) {
          state.value = next
          error.value = null
          if (msg.type === 'snapshot') clearRestFallback()
        }
        break
      }
      case 'signal':
        lastSignal.value = msg.signal
        break
      case 'account':
        accounts.applyAccount(msg.account)
        break
      case 'ping':
        break
    }
  }

  async function loadRest(forTf: Timeframe = state.value.tf): Promise<void> {
    try {
      const res = await api.screener(forTf)
      if (forTf !== state.value.tf) return
      state.value = { tf: forTf, rows: res.rows, updatedAt: res.updated_at, ready: true }
      error.value = null
    } catch (e) {
      error.value = errorMessage(e)
    }
  }

  function start(): void {
    if (socket) return
    socket = new ScreenerSocket(state.value.tf, {
      onMessage: handleMessage,
      onState: (s, info) => {
        wsState.value = s
        wsAttempt.value = info.attempt
        wsNextRetryMs.value = info.nextRetryMs
      },
      onUnauthorized: () => {
        // WS закрыт кодом 4401 — сессии нет: на экран логина
        auth.markLoggedOut()
        notifyUnauthorized()
      },
    })
    socket.start()
    // если WS не поднялся быстро (сервер WS недоступен) — показываем данные REST
    scheduleRestFallback(1500)
  }

  function clearRestFallback(): void {
    if (restFallbackTimer) clearTimeout(restFallbackTimer)
    restFallbackTimer = null
  }

  function stop(): void {
    socket?.stop()
    socket = null
    clearRestFallback()
  }

  function scheduleRestFallback(ms: number): void {
    if (restFallbackTimer) clearTimeout(restFallbackTimer)
    const want = state.value.tf
    restFallbackTimer = setTimeout(() => {
      restFallbackTimer = null
      if (state.value.tf === want && wsState.value !== 'open') void loadRest(want)
    }, ms)
  }

  function setTf(next: Timeframe): void {
    if (next === state.value.tf) return
    state.value = { tf: next, rows: state.value.rows, updatedAt: state.value.updatedAt, ready: false }
    // по открытому WS сервер ответит snapshot нового ТФ; без WS — REST
    socket?.setTimeframe(next)
    if (!socket?.isOpen) {
      clearRestFallback()
      void loadRest(next)
    }
  }

  function setFilters(f: ScreenerFilters): void {
    filters.value = cloneFilters(f)
  }

  function resetFilters(): void {
    filters.value = cloneFilters(DEFAULT_FILTERS)
  }

  function savePreset(name: string): void {
    presets.value = upsertPreset(presets.value, name, filters.value)
  }

  function applyPreset(name: string): void {
    const p = presets.value.find((x) => x.name === name)
    if (p) filters.value = cloneFilters(p.filters)
  }

  function deletePreset(name: string): void {
    presets.value = presets.value.filter((p) => p.name !== name)
  }

  function findRow(sym: string): ScreenerRow | undefined {
    return state.value.rows.find((r) => r.hh_symbol === sym)
  }

  return {
    state,
    tf,
    wsState,
    wsAttempt,
    wsNextRetryMs,
    lastMessageAt,
    error,
    filters,
    presets,
    lastSignal,
    calibrationStale,
    filteredRows,
    handleMessage,
    loadRest,
    start,
    stop,
    setTf,
    setFilters,
    resetFilters,
    savePreset,
    applyPreset,
    deletePreset,
    findRow,
  }
})
