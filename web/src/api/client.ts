// Базовый HTTP-клиент: fetch + credentials: "include", JSON, единая обработка 401.

export class ApiError extends Error {
  readonly status: number
  readonly detail: string

  constructor(status: number, detail: string) {
    super(detail || `HTTP ${status}`)
    this.name = 'ApiError'
    this.status = status
    this.detail = detail
  }
}

type UnauthorizedHandler = () => void

let onUnauthorized: UnauthorizedHandler | null = null

/** Регистрирует обработчик 401 (редирект на /login). Вызывается из main.ts. */
export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  onUnauthorized = handler
}

/** Сообщить о потере сессии вне HTTP (например, WS закрыт с кодом 4401). */
export function notifyUnauthorized(): void {
  onUnauthorized?.()
}

export type QueryValue = string | number | boolean | null | undefined

export function buildQuery(params: Record<string, QueryValue> | undefined): string {
  if (!params) return ''
  const qs = new URLSearchParams()
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue
    qs.set(k, String(v))
  }
  const s = qs.toString()
  return s ? `?${s}` : ''
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PATCH' | 'DELETE'
  body?: unknown
  query?: Record<string, QueryValue>
  signal?: AbortSignal
  /** не вызывать глобальный обработчик 401 (для логина и первичной проверки сессии) */
  skipAuthRedirect?: boolean
  /** ожидаемый тип ответа */
  as?: 'json' | 'text'
}

async function extractDetail(res: Response): Promise<string> {
  try {
    const text = await res.text()
    if (!text) return res.statusText
    try {
      const data = JSON.parse(text) as { detail?: unknown }
      if (typeof data.detail === 'string') return data.detail
      if (Array.isArray(data.detail)) {
        // FastAPI validation errors
        return data.detail
          .map((d: { msg?: string }) => (typeof d?.msg === 'string' ? d.msg : JSON.stringify(d)))
          .join('; ')
      }
      return text
    } catch {
      return text
    }
  } catch {
    return res.statusText
  }
}

export async function request<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const url = path + buildQuery(opts.query)
  const headers: Record<string, string> = { Accept: opts.as === 'text' ? 'text/html' : 'application/json' }
  let body: BodyInit | undefined
  if (opts.body !== undefined) {
    headers['Content-Type'] = 'application/json'
    body = JSON.stringify(opts.body)
  }

  let res: Response
  try {
    res = await fetch(url, {
      method: opts.method ?? 'GET',
      credentials: 'include',
      headers,
      body,
      signal: opts.signal,
    })
  } catch (e) {
    if (e instanceof DOMException && e.name === 'AbortError') throw e
    throw new ApiError(0, 'Нет связи с сервером')
  }

  if (res.status === 401) {
    const detail = await extractDetail(res)
    if (!opts.skipAuthRedirect && onUnauthorized) onUnauthorized()
    throw new ApiError(401, detail || 'unauthorized')
  }
  if (!res.ok) {
    throw new ApiError(res.status, await extractDetail(res))
  }
  if (opts.as === 'text') return (await res.text()) as T
  if (res.status === 204) return undefined as T
  return (await res.json()) as T
}

export const http = {
  get: <T>(path: string, query?: Record<string, QueryValue>, signal?: AbortSignal) =>
    request<T>(path, { query, signal }),
  post: <T>(path: string, body?: unknown) => request<T>(path, { method: 'POST', body }),
  patch: <T>(path: string, body?: unknown) => request<T>(path, { method: 'PATCH', body }),
  del: <T>(path: string, query?: Record<string, QueryValue>) => request<T>(path, { method: 'DELETE', query }),
}

/** Человекочитаемое сообщение об ошибке для UI. */
export function errorMessage(e: unknown): string {
  if (e instanceof ApiError) {
    if (e.status === 0) return e.detail
    if (e.status === 401) return 'Сессия истекла — войдите снова'
    if (e.status === 404) return `Не найдено (${e.detail})`
    return `Ошибка ${e.status}: ${e.detail}`
  }
  if (e instanceof Error) return e.message
  return String(e)
}
