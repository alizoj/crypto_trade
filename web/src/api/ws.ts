// WebSocket /ws/screener: переподключение с экспоненциальным backoff и jitter,
// контроль «тишины» (сервер шлёт ping раз в 20 с), переключение ТФ без переподключения.
import { WS_CLOSE_UNAUTHORIZED, type Timeframe, type WsClientMessage, type WsServerMessage } from './types'

export type WsState = 'connecting' | 'open' | 'reconnecting' | 'closed'

export interface BackoffOptions {
  baseMs: number
  maxMs: number
  factor: number
  /** 0..1 — доля случайного разброса */
  jitter: number
}

export const DEFAULT_BACKOFF: BackoffOptions = { baseMs: 1000, maxMs: 30000, factor: 2, jitter: 0.25 }

/** Задержка перед попыткой `attempt` (0 — первая повторная попытка). */
export function backoffDelay(attempt: number, opts: BackoffOptions = DEFAULT_BACKOFF, rnd: () => number = Math.random): number {
  const raw = Math.min(opts.maxMs, opts.baseMs * Math.pow(opts.factor, Math.max(0, attempt)))
  const spread = raw * opts.jitter
  return Math.max(0, Math.round(raw - spread + rnd() * 2 * spread))
}

export function wsUrl(path: string, loc: Pick<Location, 'protocol' | 'host'> = window.location): string {
  const proto = loc.protocol === 'https:' ? 'wss:' : 'ws:'
  return `${proto}//${loc.host}${path}`
}

export interface ScreenerSocketHandlers {
  onMessage: (msg: WsServerMessage) => void
  onState?: (state: WsState, info: { attempt: number; nextRetryMs: number | null }) => void
  /** сервер закрыл соединение с кодом 4401 (нет сессии); переподключения нет до reconnectNow() */
  onUnauthorized?: () => void
}

/** Если за это время не пришло ни одного сообщения — считаем соединение мёртвым. */
const SILENCE_TIMEOUT_MS = 45000

export class ScreenerSocket {
  private ws: WebSocket | null = null
  private tf: Timeframe
  private attempt = 0
  private retryTimer: ReturnType<typeof setTimeout> | null = null
  private silenceTimer: ReturnType<typeof setTimeout> | null = null
  private stopped = true
  private state: WsState = 'closed'
  private readonly handlers: ScreenerSocketHandlers
  private readonly backoff: BackoffOptions

  constructor(tf: Timeframe, handlers: ScreenerSocketHandlers, backoff: BackoffOptions = DEFAULT_BACKOFF) {
    this.tf = tf
    this.handlers = handlers
    this.backoff = backoff
  }

  get isOpen(): boolean {
    return this.ws !== null && this.ws.readyState === WebSocket.OPEN
  }

  get currentState(): WsState {
    return this.state
  }

  start(): void {
    if (!this.stopped) return
    this.stopped = false
    this.attempt = 0
    this.connect()
  }

  stop(): void {
    this.stopped = true
    this.clearTimers()
    if (this.ws) {
      const ws = this.ws
      this.ws = null
      ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null
      try {
        ws.close(1000, 'client stop')
      } catch {
        /* уже закрыт */
      }
    }
    this.setState('closed', null)
  }

  /**
   * Переключение ТФ: по открытому соединению — сообщением {type:"tf"} (сервер отвечает snapshot нового ТФ),
   * иначе — параметром tf при следующем подключении.
   */
  setTimeframe(tf: Timeframe): void {
    this.tf = tf
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.send({ type: 'tf', tf })
    }
  }

  /** Принудительное переподключение (например, после повторного логина). */
  reconnectNow(): void {
    this.stopped = false
    this.clearTimers()
    if (this.ws) {
      const ws = this.ws
      this.ws = null
      ws.onopen = ws.onmessage = ws.onclose = ws.onerror = null
      try {
        ws.close()
      } catch {
        /* ignore */
      }
    }
    this.attempt = 0
    this.connect()
  }

  private send(msg: WsClientMessage): void {
    try {
      this.ws?.send(JSON.stringify(msg))
    } catch {
      /* соединение закрывается — ТФ уйдёт в URL при переподключении */
    }
  }

  private setState(state: WsState, nextRetryMs: number | null): void {
    this.state = state
    this.handlers.onState?.(state, { attempt: this.attempt, nextRetryMs })
  }

  private clearTimers(): void {
    if (this.retryTimer) clearTimeout(this.retryTimer)
    if (this.silenceTimer) clearTimeout(this.silenceTimer)
    this.retryTimer = null
    this.silenceTimer = null
  }

  private armSilenceTimer(): void {
    if (this.silenceTimer) clearTimeout(this.silenceTimer)
    this.silenceTimer = setTimeout(() => {
      // нет даже ping — рвём и переподключаемся
      this.ws?.close(4000, 'silence')
    }, SILENCE_TIMEOUT_MS)
  }

  private connect(): void {
    if (this.stopped) return
    this.setState(this.attempt === 0 ? 'connecting' : 'reconnecting', null)
    let ws: WebSocket
    try {
      ws = new WebSocket(wsUrl(`/ws/screener?tf=${encodeURIComponent(this.tf)}`))
    } catch {
      this.scheduleReconnect()
      return
    }
    this.ws = ws

    ws.onopen = () => {
      this.attempt = 0
      this.setState('open', null)
      this.armSilenceTimer()
    }
    ws.onmessage = (ev: MessageEvent) => {
      this.armSilenceTimer()
      let msg: WsServerMessage
      try {
        msg = JSON.parse(typeof ev.data === 'string' ? ev.data : String(ev.data)) as WsServerMessage
      } catch {
        return
      }
      this.handlers.onMessage(msg)
    }
    ws.onerror = () => {
      /* onclose придёт следом */
    }
    ws.onclose = (ev: CloseEvent) => {
      if (this.ws !== ws) return
      this.ws = null
      if (this.silenceTimer) clearTimeout(this.silenceTimer)
      if (this.stopped) return
      if (ev.code === WS_CLOSE_UNAUTHORIZED) {
        this.setState('closed', null)
        this.handlers.onUnauthorized?.()
        return
      }
      this.scheduleReconnect()
    }
  }

  private scheduleReconnect(): void {
    if (this.stopped) return
    const delay = backoffDelay(this.attempt, this.backoff)
    this.attempt += 1
    this.setState('reconnecting', delay)
    this.retryTimer = setTimeout(() => this.connect(), delay)
  }
}
