// Безопасный доступ к localStorage: любой вызов может бросить
// (приватный режим, заблокированные cookies/site data, квота).

function store(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.localStorage : null
  } catch {
    return null
  }
}

export function readString(key: string): string | null {
  try {
    return store()?.getItem(key) ?? null
  } catch {
    return null
  }
}

export function writeString(key: string, value: string): boolean {
  try {
    const s = store()
    if (!s) return false
    s.setItem(key, value)
    return true
  } catch {
    return false
  }
}

export function removeKey(key: string): void {
  try {
    store()?.removeItem(key)
  } catch {
    /* ignore */
  }
}

export function readJSON<T>(key: string, fallback: T, validate?: (v: unknown) => v is T): T {
  const raw = readString(key)
  if (raw === null) return fallback
  try {
    const v = JSON.parse(raw) as unknown
    if (validate && !validate(v)) return fallback
    return v as T
  } catch {
    return fallback
  }
}

export function writeJSON(key: string, value: unknown): boolean {
  try {
    return writeString(key, JSON.stringify(value))
  } catch {
    return false
  }
}

export const STORAGE_KEYS = {
  theme: 'hh.theme',
  tz: 'hh.tz',
  account: 'hh.account',
  screenerState: 'hh.screener.state',
  screenerPresets: 'hh.screener.presets',
  instrumentOverlays: 'hh.instrument.overlays',
} as const
