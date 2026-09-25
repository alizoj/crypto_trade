import { test, expect } from '@playwright/test'
import { apiGet, expectDisplayed, freezeMock, loginOk, trackErrors } from './helpers'

interface Prob {
  p: number
  n: number
  calibrated: boolean
  low_data: boolean
}
interface Plan {
  entry: number
  sl: number
  tp1: number
  tp2: number | null
  notional_usd: number | null
  risk_usd: number | null
  blocked_reason: string | null
}
interface SideA {
  score: number | null
  prob: Prob | null
  ev_r: number | null
  plan: Plan | null
  factors: { name: string }[]
}
interface Analysis {
  long: SideA
  short: SideA
  text: string[]
  llm_summary: string | null
}
interface Bars {
  bars: { t: number; c: number }[]
  active_signal: { id: number } | null
}

test.describe('Экран инструмента', () => {
  test.beforeEach(async ({ page }) => {
    await freezeMock(page)
    await loginOk(page)
  })

  test('переход из скринера, график (canvas) и числа анализа = API', async ({ page }) => {
    const errors = trackErrors(page)
    await expect(page.getByTestId('screener-row').first()).toBeVisible()
    // первый инструмент со статусом «сигнал»
    const row = page.locator('[data-testid="screener-row"]:has([data-status="signal"])').first()
    const sym = await row.getAttribute('data-symbol')
    expect(sym).toBeTruthy()
    await row.locator('td[data-col="symbol"] a').click()
    await expect(page).toHaveURL(new RegExp(`/instrument/${sym}$`))
    await expect(page.getByTestId('instrument-symbol')).toHaveText(sym!)

    // график: canvas Lightweight Charts отрисован и имеет размер
    const canvas = page.locator('[data-testid="price-chart"] canvas').first()
    await expect(canvas).toBeVisible()
    const box = await canvas.boundingBox()
    expect(box!.width).toBeGreaterThan(200)
    expect(box!.height).toBeGreaterThan(100)
    // три панели (цена, RSI, объём/CVD) — несколько canvas
    expect(await page.locator('[data-testid="price-chart"] canvas').count()).toBeGreaterThanOrEqual(6)

    const bars = await apiGet<Bars>(page, `/api/instruments/${sym}/bars?tf=1h&limit=500`)
    expect(bars.bars.length).toBeGreaterThanOrEqual(300)
    expect(bars.active_signal).not.toBeNull()
    expectDisplayed(await page.getByTestId('instrument-price').innerText(), bars.bars.at(-1)!.c, 1, 'price')

    const a = await apiGet<Analysis>(page, `/api/instruments/${sym}/analysis?tf=1h&account_id=1`)
    await expect(page.getByTestId('analysis-panel')).toBeVisible()
    for (const side of ['long', 'short'] as const) {
      const card = page.getByTestId(`prob-${side}`)
      const p = a[side].prob!
      const label = await card.locator('[data-part="label"]').innerText()
      if (!p.calibrated) expectDisplayed(label, a[side].score, 1, `${side} score`)
      else if (p.low_data) expect(label).toBe('мало данных')
      else expectDisplayed(label, p.p, 100, `${side} P`)
      expectDisplayed(await card.locator('[data-field="ev"]').innerText(), a[side].ev_r, 1, `${side} EV`)

      const plan = page.getByTestId(`plan-${side}`)
      const pl = a[side].plan!
      expectDisplayed(await plan.locator('[data-field="entry"]').innerText(), pl.entry, 1, `${side} entry`)
      expectDisplayed(await plan.locator('[data-field="sl"]').innerText(), pl.sl, 1, `${side} sl`)
      expectDisplayed(await plan.locator('[data-field="tp1"]').innerText(), pl.tp1, 1, `${side} tp1`)
      expectDisplayed(await plan.locator('[data-field="notional"]').innerText(), pl.notional_usd, 1, `${side} notional`)
      expectDisplayed(await plan.locator('[data-field="risk"]').innerText(), pl.risk_usd, 1, `${side} risk`)
      if (pl.blocked_reason) await expect(plan.getByTestId('plan-blocked')).toContainText(pl.blocked_reason)
      await expect(plan).toContainText('stop-market')
    }
    // таблица факторов и текстовый разбор
    await expect(page.locator('[data-testid="factors-table"] tbody tr')).toHaveCount(a.long.factors.length)
    await expect(page.locator('[data-testid="analysis-text"] li')).toHaveCount(a.text.length)
    expect(errors).toEqual([])
  })

  test('LLM-резюме помечено, контекст и смена ТФ', async ({ page }) => {
    await page.goto('/instrument/BTCUSDT')
    await expect(page.getByTestId('llm-summary')).toContainText('LLM-резюме')
    await expect(page.getByTestId('context-panel')).toContainText('Funding')
    await page.getByTestId('inst-tf-4h').click()
    await expect(page.getByTestId('prob-panel')).toContainText('4h')
    await expect(page.locator('[data-testid="price-chart"] canvas').first()).toBeVisible()
    const a = await apiGet<Analysis>(page, '/api/instruments/BTCUSDT/analysis?tf=4h&account_id=1')
    const p = a.long.prob!
    const label = await page.getByTestId('prob-long').locator('[data-part="label"]').innerText()
    if (p.calibrated && !p.low_data) expectDisplayed(label, p.p, 100, 'P 4h')
  })

  test('TradFi вне сессии: статус сессии и причина блокировки', async ({ page }) => {
    await page.goto('/instrument/TSLAUSDT')
    await expect(page.getByTestId('context-panel')).toContainText('Сессия базового рынка')
    await expect(page.getByTestId('plan-long').getByTestId('plan-blocked')).toContainText('сессии')
    await expect(page.getByTestId('prob-long')).toContainText('не откалибр')
  })
})
