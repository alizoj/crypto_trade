import { expect, type Page } from '@playwright/test'

/** Отключает live-изменения mock-данных (WS update/account), чтобы сравнивать UI с API без гонок. */
export async function freezeMock(page: Page): Promise<void> {
  await page.addInitScript(() => {
    ;(window as unknown as { __HH_MOCK_FREEZE__?: boolean }).__HH_MOCK_FREEZE__ = true
  })
}

/** Собирает необработанные ошибки страницы — тест падает, если они были. */
export function trackErrors(page: Page): string[] {
  const errors: string[] = []
  page.on('pageerror', (e) => errors.push(String(e)))
  page.on('console', (m) => {
    if (m.type() === 'error' && !/Failed to load resource/.test(m.text())) errors.push(m.text())
  })
  return errors
}

export async function login(page: Page): Promise<void> {
  await page.goto('/login')
  await page.getByLabel('Имя пользователя').fill('trader')
  await page.getByLabel('Пароль').fill('secret')
  await page.getByRole('button', { name: 'Войти' }).click()
}

export async function loginOk(page: Page): Promise<void> {
  await login(page)
  await expect(page).toHaveURL(/\/$/)
  await expect(page.getByTestId('ws-status')).toHaveAttribute('data-state', 'open')
}

/** GET к API из контекста страницы (mock перехватывает fetch в браузере; для реального бэкенда — та же cookie). */
export async function apiGet<T>(page: Page, path: string): Promise<T> {
  return page.evaluate(async (p) => {
    const r = await fetch(p, { credentials: 'include' })
    if (!r.ok) throw new Error(`${p}: HTTP ${r.status}`)
    return r.json()
  }, path) as Promise<T>
}

/** Разбор числа из текста UI: «-1 234.5%», «+0.38R», «$12 500», «—» → null. */
export function num(text: string | null): number | null {
  if (text === null) return null
  const t = text.replace(/[\s  $%R+]/g, '').replace(/−/g, '-')
  if (!t || t === '—') return null
  const n = Number(t)
  return Number.isFinite(n) ? n : null
}

/** Число знаков после точки в отображённом значении. */
export function decimalsOf(text: string): number {
  const m = /\.(\d+)/.exec(text.replace(/[\s ]/g, ''))
  return m ? m[1]!.length : 0
}

/** Совпадение отображённого числа с API-значением с точностью до округления отображения. */
export function expectDisplayed(text: string, apiValue: number | null, scale = 1, label = ''): void {
  if (apiValue === null) {
    expect(text.trim(), `${label}: ожидалось «—»`).toBe('—')
    return
  }
  const shown = num(text)
  expect(shown, `${label}: «${text}» не число`).not.toBeNull()
  const tol = 0.5 * Math.pow(10, -decimalsOf(text)) + 1e-12
  expect(Math.abs(shown! - apiValue * scale), `${label}: UI «${text}» vs API ${apiValue * scale}`).toBeLessThanOrEqual(tol)
}

declare global {
  interface Window {
    __HH_MOCK_FREEZE__?: boolean
    __HH_MOCK_STALE__?: boolean
    __hhMock?: {
      sent: unknown[]
      freeze(v: boolean): void
      frozen(): boolean
      dropWs(): void
      logout(): void
      setCalibrationStale(v: boolean): void
      openSockets(): number
    }
  }
}

/** Проверка отсутствия горизонтального скролла страницы. */
export async function expectNoHorizontalScroll(page: Page, label = ''): Promise<void> {
  const { sw, cw, culprits } = await page.evaluate(() => {
    const cw = document.documentElement.clientWidth
    // элементы, выходящие за правый край вне контейнеров с собственным скроллом
    const culprits: string[] = []
    document.querySelectorAll('body *').forEach((el) => {
      const r = el.getBoundingClientRect()
      if (r.right <= cw + 1 || culprits.length >= 5) return
      let p = el.parentElement
      while (p && p !== document.body) {
        const ox = getComputedStyle(p).overflowX
        if (ox === 'auto' || ox === 'scroll' || ox === 'hidden') return
        p = p.parentElement
      }
      culprits.push(`${el.tagName.toLowerCase()}.${String(el.className).slice(0, 60)} right=${Math.round(r.right)}`)
    })
    return { sw: document.documentElement.scrollWidth, cw, culprits }
  })
  expect(sw, `${label}: scrollWidth ${sw} > clientWidth ${cw}; ${culprits.join(' | ')}`).toBeLessThanOrEqual(cw)
}
