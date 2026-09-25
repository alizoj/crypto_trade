// Мобильная ширина 390px: карточки вместо таблицы, без горизонтального скролла страницы.
import { test, expect } from '@playwright/test'
import { apiGet, expectDisplayed, expectNoHorizontalScroll, freezeMock, loginOk } from './helpers'

interface Row {
  hh_symbol: string
  price: number | null
  change_24h_pct: number | null
  rsi14: number | null
  ev_long_r: number | null
  ev_short_r: number | null
  score_long: number | null
  funding_rate: number | null
}

test.beforeEach(async ({ page }) => {
  await freezeMock(page)
})

test('логин и скринер карточками на 390px', async ({ page }) => {
  await page.goto('/login')
  await expectNoHorizontalScroll(page)
  await loginOk(page)
  expect(page.viewportSize()!.width).toBe(390)
  await expect(page.getByTestId('screener-card').first()).toBeVisible()
  await expect(page.getByTestId('screener-table')).toHaveCount(0)
  const api = await apiGet<{ rows: Row[] }>(page, '/api/screener?tf=1h')
  await expect(page.getByTestId('screener-card')).toHaveCount(api.rows.length)
  await expectNoHorizontalScroll(page)

  // значения карточек совпадают с API
  const byS = new Map(api.rows.map((r) => [r.hh_symbol, r]))
  const cards = page.getByTestId('screener-card')
  for (let i = 0; i < 12; i++) {
    const card = cards.nth(i)
    const r = byS.get((await card.getAttribute('data-symbol'))!)!
    expectDisplayed(await card.locator('[data-col="price"]').innerText(), r.price, 1, 'price')
    expectDisplayed(await card.locator('[data-col="change"]').innerText(), r.change_24h_pct, 1, 'change')
    expectDisplayed(await card.locator('[data-col="rsi"]').innerText(), r.rsi14, 1, 'rsi')
    expectDisplayed(await card.locator('[data-col="ev_long"]').innerText(), r.ev_long_r, 1, 'ev_long')
    expectDisplayed(await card.locator('[data-col="ev_short"]').innerText(), r.ev_short_r, 1, 'ev_short')
    expectDisplayed(await card.locator('[data-col="score_long"]').innerText(), r.score_long, 1, 'score_long')
    expectDisplayed(await card.locator('[data-col="funding"]').innerText(), r.funding_rate, 100, 'funding')
  }

  // фильтр работает и на телефоне (панель фильтров свёрнута)
  await expect(page.getByTestId('only-signals')).toHaveCount(0)
  await page.getByTestId('filters-toggle').click()
  await page.getByTestId('only-signals').check()
  await expect(page.getByTestId('filters-toggle')).toContainText('(1)')
  await expect(page.getByTestId('screener-card')).toHaveCount(api.rows.filter((r) => (r as unknown as { status: string }).status === 'signal').length)
  await expectNoHorizontalScroll(page)
})

test('остальные экраны без горизонтального скролла на 390px', async ({ page }) => {
  await loginOk(page)
  for (const path of ['/instrument/BTCUSDT', '/accounts', '/signals', '/backtests']) {
    await page.goto(path)
    await page.waitForLoadState('networkidle')
    if (path.startsWith('/instrument')) await expect(page.locator('[data-testid="price-chart"] canvas').first()).toBeVisible()
    if (path === '/accounts') await expect(page.getByTestId('account-card').first()).toBeVisible()
    if (path === '/signals') await expect(page.getByTestId('signal-row').first()).toBeVisible()
    if (path === '/backtests') await expect(page.getByTestId('run-row').first()).toBeVisible()
    await expectNoHorizontalScroll(page, path)
  }
})
