// /api/meta + часы сервера + обратный отсчёт до окна сброса.
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '@/api/endpoints'
import type { AssetClass, Meta, Timeframe } from '@/api/types'
import { clockOffset, resetState, type ResetWindow } from '@/utils/reset'

const FALLBACK_WINDOW: ResetWindow = { start_utc: '20:00', end_utc: '20:10' }
const FALLBACK_TFS: Timeframe[] = ['15m', '1h', '4h']
const FALLBACK_CLASSES: AssetClass[] = ['crypto', 'equity', 'etf', 'metal', 'energy']

export const useMetaStore = defineStore('meta', () => {
  const meta = ref<Meta | null>(null)
  const offsetMs = ref(0)
  const nowMs = ref(Date.now())
  const error = ref<string | null>(null)
  let timer: ReturnType<typeof setInterval> | null = null

  async function load(): Promise<void> {
    try {
      const m = await api.meta()
      meta.value = m
      offsetMs.value = clockOffset(m.server_time, Date.now())
      error.value = null
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    }
  }

  function startClock(): void {
    if (timer) return
    nowMs.value = Date.now()
    timer = setInterval(() => {
      nowMs.value = Date.now()
    }, 1000)
  }

  function stopClock(): void {
    if (timer) clearInterval(timer)
    timer = null
  }

  const serverNowMs = computed(() => nowMs.value + offsetMs.value)
  const window = computed<ResetWindow>(() => meta.value?.reset_window ?? FALLBACK_WINDOW)
  const reset = computed(() => resetState(serverNowMs.value, window.value))
  const timeframes = computed<Timeframe[]>(() => {
    const tfs = meta.value?.timeframes?.filter((t): t is Timeframe => FALLBACK_TFS.includes(t))
    return tfs && tfs.length ? tfs : FALLBACK_TFS
  })
  const assetClasses = computed<AssetClass[]>(() =>
    meta.value?.asset_classes?.length ? meta.value.asset_classes : FALLBACK_CLASSES,
  )

  return { meta, offsetMs, nowMs, serverNowMs, error, window, reset, timeframes, assetClasses, load, startClock, stopClock }
})
