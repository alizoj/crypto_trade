// Обратный отсчёт до окна дневного сброса (Crypto+TradFi: 20:00–20:10 UTC).

export interface ResetWindow {
  start_utc: string // "HH:MM"
  end_utc: string
}

export interface ResetState {
  /** сейчас идёт окно сброса */
  inWindow: boolean
  /** мс до начала следующего окна (если inWindow — до начала следующего дня) */
  msToStart: number
  /** мс до конца текущего окна (только если inWindow) */
  msToEnd: number | null
  /** начало ближайшего (или текущего) окна, мс epoch */
  nextStartMs: number
}

function parseHm(hm: string): number {
  const m = /^(\d{1,2}):(\d{2})$/.exec(hm.trim())
  if (!m) throw new Error(`bad HH:MM: ${hm}`)
  return (Number(m[1]) * 60 + Number(m[2])) * 60_000
}

const DAY = 86_400_000

/** Состояние окна сброса на момент nowMs (UTC epoch, уже с поправкой на часы сервера). */
export function resetState(nowMs: number, win: ResetWindow): ResetState {
  const start = parseHm(win.start_utc)
  let end = parseHm(win.end_utc)
  if (end <= start) end += DAY // окно через полночь
  const dayStart = Math.floor(nowMs / DAY) * DAY
  // кандидаты: вчерашнее, сегодняшнее окно (на случай перехода через полночь)
  for (const base of [dayStart - DAY, dayStart]) {
    const s = base + start
    const e = base + end
    if (nowMs >= s && nowMs < e) {
      return { inWindow: true, msToStart: s + DAY - nowMs, msToEnd: e - nowMs, nextStartMs: s }
    }
  }
  let next = dayStart + start
  if (next <= nowMs) next += DAY
  return { inWindow: false, msToStart: next - nowMs, msToEnd: null, nextStartMs: next }
}

/** Смещение часов клиента относительно сервера (мс): serverNow ≈ Date.now() + offset. */
export function clockOffset(serverTimeIso: string, clientNowMs: number): number {
  const s = Date.parse(serverTimeIso)
  return Number.isFinite(s) ? s - clientNowMs : 0
}
