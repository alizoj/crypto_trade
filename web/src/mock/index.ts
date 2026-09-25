// Mock-режим (VITE_MOCK=1): перехват fetch('/api/…') и WebSocket('/ws/…') в браузере.
// Логин: любое имя и пароль.
import type { Timeframe, WsServerMessage } from '@/api/types'
import { MOCK_SPECS } from './instruments'
import { TFS, tick } from './market'
import { getDb, iso, replaceAccount } from './data'
import { handle, mockSessionUser, screenerResponse, setMockCalibrationStale, setMockSession } from './server'
import { rng } from './math'
import { allRows } from './market'

declare global {
  interface Window {
    /** выставляется до загрузки приложения (e2e), чтобы отключить live-изменения данных */
    __HH_MOCK_FREEZE__?: boolean
    /** выставляется до загрузки приложения (e2e): /api/meta.calibration_stale = true */
    __HH_MOCK_STALE__?: boolean
    __hhMock?: MockControl
  }
}

export interface MockControl {
  /** сообщения, отправленные клиентом в WS */
  sent: unknown[]
  freeze(v: boolean): void
  frozen(): boolean
  /** разорвать все WS-соединения (проверка переподключения) */
  dropWs(): void
  /** сбросить mock-сессию (проверка 401) */
  logout(): void
  /** /api/meta.calibration_stale */
  setCalibrationStale(v: boolean): void
  openSockets(): number
}

const LATENCY_MIN = 40
const LATENCY_MAX = 140
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms))

const liveRng = rng(Date.now() & 0xffffffff)
let frozen = typeof window !== 'undefined' && window.__HH_MOCK_FREEZE__ === true
const sockets = new Set<MockWebSocket>()
const sent: unknown[] = []

function isMockPath(pathname: string, prefix: string): boolean {
  return pathname === prefix.slice(0, -1) || pathname.startsWith(prefix)
}

// ---------------- WebSocket ----------------

class MockWebSocket extends EventTarget {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  readonly CONNECTING = 0
  readonly OPEN = 1
  readonly CLOSING = 2
  readonly CLOSED = 3

  readonly url: string
  readyState = 0
  protocol = ''
  extensions = ''
  binaryType: BinaryType = 'blob'
  bufferedAmount = 0
  onopen: ((ev: Event) => void) | null = null
  onmessage: ((ev: MessageEvent) => void) | null = null
  onclose: ((ev: CloseEvent) => void) | null = null
  onerror: ((ev: Event) => void) | null = null

  private tf: Timeframe
  private timers: ReturnType<typeof setInterval>[] = []

  constructor(url: string) {
    super()
    this.url = url
    const u = new URL(url, window.location.href)
    const tf = (u.searchParams.get('tf') ?? '1h') as Timeframe
    this.tf = TFS.includes(tf) ? tf : '1h'
    setTimeout(() => this.boot(), 60)
  }

  private boot(): void {
    if (this.readyState !== 0) return
    if (!mockSessionUser()) {
      // нет сессии — сервер закрывает соединение кодом 4401 (контракт)
      this.finish(4401, 'unauthorized')
      return
    }
    this.readyState = 1
    sockets.add(this)
    this.fire('open', new Event('open'))
    this.sendSnapshot()
    this.timers.push(setInterval(() => this.pushUpdate(), 2000))
    this.timers.push(setInterval(() => this.emit({ type: 'ping', ts: iso(Date.now()) }), 20000))
    this.timers.push(setInterval(() => this.pushAccounts(), 10000))
  }

  private fire(type: 'open' | 'message' | 'close' | 'error', ev: Event): void {
    this.dispatchEvent(ev)
    const h = (this as unknown as Record<string, ((e: Event) => void) | null>)[`on${type}`]
    if (h) h.call(this as unknown as WebSocket, ev)
  }

  private emit(msg: WsServerMessage): void {
    if (this.readyState !== 1) return
    this.fire('message', new MessageEvent('message', { data: JSON.stringify(msg) }))
  }

  private sendSnapshot(): void {
    const res = screenerResponse(this.tf)
    this.emit({ type: 'snapshot', tf: this.tf, updated_at: res.updated_at, rows: res.rows })
  }

  private pushUpdate(): void {
    if (frozen) return
    const specs = MOCK_SPECS.filter((s) => s.cls === 'crypto' || s.cls === 'metal' || s.cls === 'energy')
    const changed = new Set<string>()
    for (let i = 0; i < 5; i++) {
      const s = specs[Math.floor(liveRng() * specs.length)]!
      tick(s.sym, liveRng)
      changed.add(s.sym)
    }
    const rows = allRows(this.tf).filter((r) => changed.has(r.hh_symbol)).map((r) => ({ ...r }))
    this.emit({ type: 'update', tf: this.tf, updated_at: iso(Date.now()), rows })
  }

  private pushAccounts(): void {
    if (frozen) return
    const d = getDb()
    d.accounts = d.accounts.map((a) => replaceAccount(a))
    for (const a of d.accounts) if (a.positions.length) this.emit({ type: 'account', account: a })
  }

  send(data: string): void {
    if (this.readyState !== 1) throw new DOMException('WebSocket is not open', 'InvalidStateError')
    let msg: unknown
    try {
      msg = JSON.parse(data)
    } catch {
      return
    }
    sent.push(msg)
    const m = msg as { type?: string; tf?: string }
    if (m.type === 'tf' && TFS.includes(m.tf as Timeframe)) {
      this.tf = m.tf as Timeframe
      setTimeout(() => this.sendSnapshot(), 80)
    }
  }

  close(code = 1000, reason = ''): void {
    if (this.readyState >= 2) return
    this.readyState = 2
    setTimeout(() => this.finish(code, reason), 10)
  }

  private finish(code: number, reason: string): void {
    this.timers.forEach(clearInterval)
    this.timers = []
    sockets.delete(this)
    this.readyState = 3
    this.fire('close', new CloseEvent('close', { code, reason, wasClean: code === 1000 }))
  }

  /** обрыв со стороны «сервера» */
  drop(): void {
    if (this.readyState === 1) this.finish(1006, 'mock drop')
  }
}

// ---------------- установка ----------------

export function installMock(): void {
  if (window.__HH_MOCK_STALE__ === true) setMockCalibrationStale(true)
  const origFetch = window.fetch.bind(window)
  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const rawUrl = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url
    const url = new URL(rawUrl, window.location.href)
    if (url.origin !== window.location.origin || !isMockPath(url.pathname, '/api/')) return origFetch(input, init)
    const method = (init?.method ?? (input instanceof Request ? input.method : 'GET')).toUpperCase()
    let body: unknown = undefined
    if (typeof init?.body === 'string' && init.body) {
      try {
        body = JSON.parse(init.body)
      } catch {
        return new Response(JSON.stringify({ detail: 'invalid JSON' }), { status: 400, headers: { 'Content-Type': 'application/json' } })
      }
    }
    await sleep(LATENCY_MIN + Math.random() * (LATENCY_MAX - LATENCY_MIN))
    if (init?.signal?.aborted) throw new DOMException('Aborted', 'AbortError')
    let res
    try {
      res = handle({ method, path: url.pathname, query: url.searchParams, body })
    } catch (e) {
      console.error('[mock] handler error', e)
      res = { status: 500, body: { detail: String(e) } }
    }
    const isText = res.contentType?.startsWith('text/')
    return new Response(isText ? String(res.body) : JSON.stringify(res.body), {
      status: res.status,
      headers: { 'Content-Type': res.contentType ?? 'application/json' },
    })
  }

  const OrigWS = window.WebSocket
  const WsProxy = new Proxy(OrigWS, {
    construct(target, args: [string | URL, (string | string[])?]) {
      const u = new URL(String(args[0]), window.location.href)
      if (isMockPath(u.pathname, '/ws/')) return new MockWebSocket(u.href) as unknown as WebSocket
      return new target(...(args as [string | URL, (string | string[])?]))
    },
  })
  window.WebSocket = WsProxy

  window.__hhMock = {
    sent,
    freeze(v: boolean) {
      frozen = v
    },
    frozen: () => frozen,
    dropWs() {
      for (const s of [...sockets]) s.drop()
    },
    logout() {
      setMockSession(null)
    },
    setCalibrationStale(v: boolean) {
      setMockCalibrationStale(v)
    },
    openSockets: () => sockets.size,
  }
  console.info('[mock] HashHedge mock-режим: логин — любое имя и пароль')
}
