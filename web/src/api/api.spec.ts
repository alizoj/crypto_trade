import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, buildQuery, errorMessage, request, setUnauthorizedHandler } from './client'
import { api } from './endpoints'
import { ScreenerSocket, backoffDelay, wsUrl, DEFAULT_BACKOFF } from './ws'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } })
}

describe('HTTP-клиент', () => {
  const fetchMock = vi.fn()
  beforeEach(() => {
    fetchMock.mockReset()
    vi.stubGlobal('fetch', fetchMock)
  })
  afterEach(() => {
    vi.unstubAllGlobals()
    setUnauthorizedHandler(null)
  })

  it('buildQuery пропускает пустые значения', () => {
    expect(buildQuery({ tf: '1h', a: '', b: null, c: undefined, limit: 50 })).toBe('?tf=1h&limit=50')
    expect(buildQuery({})).toBe('')
  })

  it('fetch с credentials: include и JSON-телом', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ id: 5 }))
    await api.decision(5, true)
    const [url, init] = fetchMock.mock.calls[0]!
    expect(url).toBe('/api/signals/5/decision')
    expect(init.credentials).toBe('include')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body)).toEqual({ taken: true })
  })

  it('параметры запроса формируются по контракту', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ total: 0, items: [] }))
    await api.signals({ status: 'tp', side: 'long', symbol: '', limit: 25, offset: 50 })
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/signals?status=tp&side=long&limit=25&offset=50')
    fetchMock.mockImplementation(async () => jsonResponse({ total: 0, items: [] }))
    await api.signals({ status: ['tp', 'sl', 'timeout'], from: '2026-09-01T00:00:00Z', to: '2026-09-02T00:00:00Z' })
    expect(decodeURIComponent(fetchMock.mock.calls[1]![0])).toBe(
      '/api/signals?status=tp,sl,timeout&from=2026-09-01T00:00:00Z&to=2026-09-02T00:00:00Z&limit=50&offset=0',
    )
    fetchMock.mockClear()
    fetchMock.mockImplementation(async () => jsonResponse({}))
    await api.bars('BTCUSDT', '4h', 500)
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/instruments/BTCUSDT/bars?tf=4h&limit=500')
    await api.closePosition(1, 7, 101.5)
    expect(fetchMock.mock.calls[1]![0]).toBe('/api/accounts/1/positions/7?exit_price=101.5')
    expect(fetchMock.mock.calls[1]![1].method).toBe('DELETE')
  })

  it('401 от любого запроса вызывает обработчик (редирект на логин)', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'unauthorized' }, 401))
    await expect(api.meta()).rejects.toMatchObject({ status: 401 })
    expect(handler).toHaveBeenCalledTimes(1)
  })

  it('401 при логине и проверке сессии не вызывает редирект', async () => {
    const handler = vi.fn()
    setUnauthorizedHandler(handler)
    fetchMock.mockResolvedValue(jsonResponse({ detail: 'неверный логин или пароль' }, 401))
    await expect(api.login({ username: 'a', password: 'b' })).rejects.toMatchObject({ detail: 'неверный логин или пароль' })
    await expect(api.me()).rejects.toBeInstanceOf(ApiError)
    expect(handler).not.toHaveBeenCalled()
  })

  it('ошибки FastAPI и сети', async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: [{ msg: 'field required' }] }, 422))
    const e = await request('/api/x').catch((x: unknown) => x)
    expect(e).toBeInstanceOf(ApiError)
    expect(errorMessage(e)).toBe('Ошибка 422: field required')
    fetchMock.mockRejectedValue(new TypeError('Failed to fetch'))
    const e2 = await request('/api/x').catch((x: unknown) => x)
    expect((e2 as ApiError).status).toBe(0)
    expect(errorMessage(e2)).toBe('Нет связи с сервером')
  })

  it('HTML-отчёт читается как текст', async () => {
    fetchMock.mockResolvedValue(new Response('<html>ok</html>', { status: 200, headers: { 'Content-Type': 'text/html' } }))
    expect(await api.backtestReport('/api/backtests/1/report')).toBe('<html>ok</html>')
  })
})

// ---------------- WebSocket ----------------

class FakeWS {
  static readonly CONNECTING = 0
  static readonly OPEN = 1
  static readonly CLOSING = 2
  static readonly CLOSED = 3
  static instances: FakeWS[] = []
  readyState = 0
  sent: string[] = []
  onopen: ((e: Event) => void) | null = null
  onmessage: ((e: MessageEvent) => void) | null = null
  onclose: ((e: CloseEvent) => void) | null = null
  onerror: ((e: Event) => void) | null = null
  constructor(public url: string) {
    FakeWS.instances.push(this)
  }
  send(d: string) {
    this.sent.push(d)
  }
  close() {
    this.readyState = 3
  }
  // управление из теста
  open() {
    this.readyState = 1
    this.onopen?.(new Event('open'))
  }
  message(obj: unknown) {
    this.onmessage?.(new MessageEvent('message', { data: JSON.stringify(obj) }))
  }
  serverClose(code = 1006) {
    this.readyState = 3
    this.onclose?.(new CloseEvent('close', { code }))
  }
}

describe('WebSocket скринера', () => {
  beforeEach(() => {
    FakeWS.instances = []
    vi.stubGlobal('WebSocket', FakeWS)
    vi.useFakeTimers()
  })
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
  })

  it('backoff: растёт экспоненциально, ограничен сверху, с разбросом', () => {
    const mid = () => 0.5
    expect(backoffDelay(0, DEFAULT_BACKOFF, mid)).toBe(1000)
    expect(backoffDelay(1, DEFAULT_BACKOFF, mid)).toBe(2000)
    expect(backoffDelay(3, DEFAULT_BACKOFF, mid)).toBe(8000)
    expect(backoffDelay(20, DEFAULT_BACKOFF, mid)).toBe(30000)
    expect(backoffDelay(0, DEFAULT_BACKOFF, () => 0)).toBe(750)
    expect(backoffDelay(0, DEFAULT_BACKOFF, () => 1)).toBe(1250)
  })

  it('wsUrl учитывает протокол страницы', () => {
    expect(wsUrl('/ws/screener?tf=1h', { protocol: 'https:', host: 'hh.example' })).toBe('wss://hh.example/ws/screener?tf=1h')
    expect(wsUrl('/ws/x', { protocol: 'http:', host: 'localhost:5173' })).toBe('ws://localhost:5173/ws/x')
  })

  it('snapshot/update, переключение ТФ сообщением и переподключение с backoff', () => {
    const msgs: unknown[] = []
    const states: string[] = []
    const sock = new ScreenerSocket('1h', { onMessage: (m) => msgs.push(m), onState: (s) => states.push(s) })
    sock.start()
    expect(FakeWS.instances).toHaveLength(1)
    const ws1 = FakeWS.instances[0]!
    expect(ws1.url).toContain('/ws/screener?tf=1h')
    ws1.open()
    expect(sock.currentState).toBe('open')
    ws1.message({ type: 'snapshot', tf: '1h', updated_at: 'x', rows: [] })
    expect(msgs).toHaveLength(1)

    sock.setTimeframe('4h')
    expect(JSON.parse(ws1.sent[0]!)).toEqual({ type: 'tf', tf: '4h' })

    ws1.serverClose(1006)
    expect(sock.currentState).toBe('reconnecting')
    vi.advanceTimersByTime(1300)
    expect(FakeWS.instances).toHaveLength(2)
    // при переподключении ТФ уходит в URL
    expect(FakeWS.instances[1]!.url).toContain('tf=4h')
    FakeWS.instances[1]!.open()
    expect(states.at(-1)).toBe('open')
    sock.stop()
    expect(sock.currentState).toBe('closed')
  })

  it('код 4401 (нет сессии) → onUnauthorized, без переподключения', () => {
    const onUnauthorized = vi.fn()
    const sock = new ScreenerSocket('1h', { onMessage: () => undefined, onUnauthorized })
    sock.start()
    FakeWS.instances[0]!.serverClose(4401)
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
    expect(sock.currentState).toBe('closed')
    vi.advanceTimersByTime(60000)
    expect(FakeWS.instances).toHaveLength(1)
    // после повторного логина — переподключение
    sock.reconnectNow()
    expect(FakeWS.instances).toHaveLength(2)
    sock.stop()
  })

  it('обрыв до открытия (сеть) — не 401, переподключение с backoff', () => {
    const onUnauthorized = vi.fn()
    const sock = new ScreenerSocket('1h', { onMessage: () => undefined, onUnauthorized })
    sock.start()
    FakeWS.instances[0]!.serverClose(1006)
    expect(onUnauthorized).not.toHaveBeenCalled()
    expect(sock.currentState).toBe('reconnecting')
    vi.advanceTimersByTime(1300)
    expect(FakeWS.instances).toHaveLength(2)
    sock.stop()
    vi.advanceTimersByTime(60000)
    expect(FakeWS.instances).toHaveLength(2) // после stop не переподключается
  })

  it('битые сообщения игнорируются', () => {
    const onMessage = vi.fn()
    const sock = new ScreenerSocket('1h', { onMessage })
    sock.start()
    const ws = FakeWS.instances[0]!
    ws.open()
    ws.onmessage?.(new MessageEvent('message', { data: '{не json' }))
    expect(onMessage).not.toHaveBeenCalled()
    sock.stop()
  })
})
