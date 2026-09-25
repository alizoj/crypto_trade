// Пользовательские настройки: тема, пояс времени, выбранный счёт. Хранятся в localStorage.
import { defineStore } from 'pinia'
import { ref, watch } from 'vue'
import type { TzMode } from '@/utils/format'
import { STORAGE_KEYS, readString, removeKey, writeString } from '@/utils/storage'

export type ThemePref = 'system' | 'light' | 'dark'

function readTheme(): ThemePref {
  const v = readString(STORAGE_KEYS.theme)
  return v === 'light' || v === 'dark' ? v : 'system'
}

function readTz(): TzMode {
  return readString(STORAGE_KEYS.tz) === 'local' ? 'local' : 'utc'
}

function readAccount(): number | null {
  const v = readString(STORAGE_KEYS.account)
  if (v === null) return null
  const n = Number(v)
  return Number.isInteger(n) && n > 0 ? n : null
}

export function applyTheme(theme: ThemePref): void {
  if (typeof document === 'undefined') return
  const el = document.documentElement
  if (theme === 'system') el.removeAttribute('data-theme')
  else el.setAttribute('data-theme', theme)
}

/** Фактическая тема с учётом системной настройки. */
export function effectiveTheme(theme: ThemePref): 'light' | 'dark' {
  if (theme !== 'system') return theme
  try {
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

export const usePrefsStore = defineStore('prefs', () => {
  const theme = ref<ThemePref>(readTheme())
  const tz = ref<TzMode>(readTz())
  const accountId = ref<number | null>(readAccount())
  /** счётчик, меняющийся при смене системной темы (для перерисовки графиков) */
  const systemThemeTick = ref(0)

  watch(
    theme,
    (t) => {
      if (t === 'system') removeKey(STORAGE_KEYS.theme)
      else writeString(STORAGE_KEYS.theme, t)
      applyTheme(t)
    },
    { immediate: true },
  )
  watch(tz, (v) => writeString(STORAGE_KEYS.tz, v))
  watch(accountId, (v) => {
    if (v === null) removeKey(STORAGE_KEYS.account)
    else writeString(STORAGE_KEYS.account, String(v))
  })

  try {
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', () => {
      systemThemeTick.value++
    })
  } catch {
    /* нет matchMedia (тесты) */
  }

  function cycleTheme(): void {
    theme.value = theme.value === 'system' ? 'light' : theme.value === 'light' ? 'dark' : 'system'
  }

  function toggleTz(): void {
    tz.value = tz.value === 'utc' ? 'local' : 'utc'
  }

  return { theme, tz, accountId, systemThemeTick, cycleTheme, toggleTz }
})
