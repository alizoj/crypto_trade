import { test, expect, type Page } from '@playwright/test'
import { apiGet, expectDisplayed, freezeMock, loginOk, num, trackErrors } from './helpers'

interface Prob {
  p: number
  ci_low: number
  ci_high: number
  n: number
  calibrated: boolean
  low_data: boolean
}
interface Row {
  hh_symbol: string
  base: string
  asset_class: string
  price: number | null
  change_24h_pct: number | null
  htf_trend: number
  rsi14: number | null
  adx: number | null
  vwap_dist_pct: number | null
  atr_pct: number | null
  funding_rate: number | null
  oi_change_24h_pct: number | null
  cvd_trend: number
  score_long: number | null
  score_short: number | null
  p_long: Prob | null
  p_short: Prob | null
  ev_long_r: number | null
  ev_short_r: number | null
  status: string
  active_signal_side: 'long' | 'short' | null
  max_per_lot_usd: number
}
interface ScreenerResp {
  tf: string
  rows: Row[]
}

interface UiCell {
  text: string
  gray: string | null
  status: string | null
  value: string | null
  tip: string | null
}
interface UiRow {
  symbol: string
  cells: Record<string, UiCell>
}

const CLASS_RU: Record<string, string> = { crypto: 'Крипто', equity: 'Акция', etf: 'ETF', metal: 'Металл', energy: 'Энергия' }

async function readTable(page: Page): Promise<UiRow[]> {
  return page.$$eval('[data-testid="screener-row"]', (trs) =>
    trs.map((tr) => {
      const cells: Record<string, UiCell> = {}
      tr.querySelectorAll('td[data-col]').forEach((td) => {
        const label = td.querySelector('[data-part="label"]')
        const probEl = td.querySelector('[data-kind]')
        cells[td.getAttribute('data-col')!] = {
          text: (label ?? td).textContent!.trim(),
          gray: probEl ? probEl.getAttribute('data-gray') : null,
          status: td.querySelector('[data-status]')?.getAttribute('data-status') ?? null,
          value: td.querySelector('[data-value]')?.getAttribute('data-value') ?? null,
          tip: probEl ? probEl.getAttribute('data-tip') : null,
        }
      })
      return { symbol: tr.getAttribute('data-symbol')!, cells }
    }),
  )
}

function parseCompactUsd(text: string): number {
  const m = /\$([\d.]+)([KM]?)/.exec(text)
  if (!m) throw new Error(`не $-значение: ${text}`)
  return Number(m[1]) * (m[2] === 'M' ? 1e6 : m[2] === 'K' ? 1e3 : 1)
}

function expectProb(cell: UiCell, prob: Prob | null, score: number | null, label: string): void {
  if (!prob) {
    expect(cell.text, label).toBe('—')
    return
  }
  if (!prob.calibrated) {
    expect(cell.gray, label).toBe('uncalibrated')
    expectDisplayed(cell.text, score, 1, `${label} (score)`)
    return
  }
  if (prob.low_data) {
    expect(cell.gray, label).toBe('low_data')
    expect(cell.text, label).toBe('мало данных')
    return
  }
  expectDisplayed(cell.text, prob.p, 100, label)
  // ДИ и n — в тултипе
  expect(cell.tip, label).toContain(`n = ${prob.n}`)
  expect(cell.tip, label).toContain(`${Math.round(prob.ci_low * 100)}–${Math.round(prob.ci_high * 100)}%`)
}

function compareRows(ui: UiRow[], api: Row[]): void {
  expect(ui.length).toBe(api.length)
  const byS = new Map(api.map((r) => [r.hh_symbol, r]))
  for (const u of ui) {
    const r = byS.get(u.symbol)
    expect(r, `строка ${u.symbol} есть в API`).toBeTruthy()
    const c = u.cells
    const L = (col: string) => `${u.symbol}.${col}`
    expect(c.symbol!.text.startsWith(r!.base), L('symbol')).toBe(true)
    expect(c.class!.text, L('class')).toBe(CLASS_RU[r!.asset_class])
    expectDisplayed(c.price!.text, r!.price, 1, L('price'))
    expectDisplayed(c.change!.text, r!.change_24h_pct, 1, L('change'))
    expect(Number(c.htf!.value), L('htf')).toBe(r!.htf_trend)
    expectDisplayed(c.rsi!.text, r!.rsi14, 1, L('rsi'))
    expectDisplayed(c.adx!.text, r!.adx, 1, L('adx'))
    expectDisplayed(c.vwap!.text, r!.vwap_dist_pct, 1, L('vwap'))
    expectDisplayed(c.atr!.text, r!.atr_pct, 1, L('atr'))
    expectDisplayed(c.funding!.text, r!.funding_rate, 100, L('funding'))
    expectDisplayed(c.oi!.text, r!.oi_change_24h_pct, 1, L('oi'))
    expect(Number(c.cvd!.value), L('cvd')).toBe(r!.cvd_trend)
    if (c.score_long) expectDisplayed(c.score_long.text, r!.score_long, 1, L('score_long'))
    if (c.score_short) expectDisplayed(c.score_short.text, r!.score_short, 1, L('score_short'))
    if (c.p_long) expectProb(c.p_long, r!.p_long, r!.score_long, L('p_long'))
    if (c.p_short) expectProb(c.p_short, r!.p_short, r!.score_short, L('p_short'))
    if (c.ev_long) expectDisplayed(c.ev_long.text, r!.ev_long_r, 1, L('ev_long'))
    if (c.ev_short) expectDisplayed(c.ev_short.text, r!.ev_short_r, 1, L('ev_short'))
    expect(c.status!.status, L('status')).toBe(r!.status)
    if (r!.status === 'signal') expect(c.status!.text, L('status side')).toContain(r!.active_signal_side === 'short' ? '▼' : '▲')
    const lim = parseCompactUsd(c.limit!.text)
    expect(Math.abs(lim - r!.max_per_lot_usd) / r!.max_per_lot_usd, L('limit')).toBeLessThan(0.051)
  }
}

test.describe('Скринер', () => {
  test.beforeEach(async ({ page }) => {
    await freezeMock(page)
    await loginOk(page)
    await expect(page.getByTestId('screener-row').first()).toBeVisible()
  })

  test('все числа в таблице совпадают с API (ТФ 1h)', async ({ page }) => {
    const errors = trackErrors(page)
    const api = await apiGet<ScreenerResp>(page, '/api/screener?tf=1h')
    expect(api.rows.length).toBeGreaterThanOrEqual(30)
    await expect(page.getByTestId('screener-row')).toHaveCount(api.rows.length)
    await expect(page.getByTestId('row-count')).toHaveText(String(api.rows.length))
    compareRows(await readTable(page), api.rows)
    // серые состояния присутствуют
    const ui = await readTable(page)
    const grays = new Set(ui.flatMap((u) => [u.cells.p_long?.gray, u.cells.p_short?.gray]))
    expect(grays).toContain('low_data')
    expect(grays).toContain('uncalibrated')
    expect(grays).toContain('off_session')
    expect(errors).toEqual([])
  })

  test('переключение ТФ: сообщение {type:"tf"} по WS и данные 4h', async ({ page }) => {
    await page.getByTestId('tf-4h').click()
    await expect(page.getByTestId('tf-4h')).toHaveAttribute('aria-pressed', 'true')
    await expect(page.getByTestId('screener-meta')).toContainText('ТФ 4h')
    const sent = await page.evaluate(() => window.__hhMock!.sent)
    expect(sent).toContainEqual({ type: 'tf', tf: '4h' })
    // одно соединение — ТФ переключён без переподключения
    expect(await page.evaluate(() => window.__hhMock!.openSockets())).toBe(1)
    const api = await apiGet<ScreenerResp>(page, '/api/screener?tf=4h')
    await expect(async () => compareRows(await readTable(page), api.rows)).toPass({ timeout: 5000 })
    // выбор ТФ сохраняется после перезагрузки
    await page.reload()
    await expect(page.getByTestId('tf-4h')).toHaveAttribute('aria-pressed', 'true')
  })

  test('фильтры: класс актива, только сигналы, min P, сторона', async ({ page }) => {
    const api = await apiGet<ScreenerResp>(page, '/api/screener?tf=1h')
    await page.getByTestId('class-metal').click()
    const metals = api.rows.filter((r) => r.asset_class === 'metal')
    await expect(page.getByTestId('screener-row')).toHaveCount(metals.length)
    for (const t of await page.locator('[data-testid="screener-row"] td[data-col="class"]').allTextContents()) expect(t.trim()).toBe('Металл')
    await page.getByTestId('class-metal').click()

    await page.getByTestId('only-signals').check()
    const signals = api.rows.filter((r) => r.status === 'signal')
    expect(signals.length).toBeGreaterThan(0)
    await expect(page.getByTestId('screener-row')).toHaveCount(signals.length)
    await page.getByTestId('only-signals').uncheck()

    await page.getByTestId('min-p').fill('55')
    await page.getByTestId('min-p').press('Enter')
    const usable = (p: Prob | null) => (p && p.calibrated && !p.low_data ? p.p : -1)
    const minP = api.rows.filter((r) => Math.max(usable(r.p_long), usable(r.p_short)) * 100 >= 55)
    await expect(page.getByTestId('screener-row')).toHaveCount(minP.length)
    await expect(page.getByTestId('reset-filters')).toBeEnabled()

    await page.getByTestId('side-short').click()
    // колонки лонга скрыты
    await expect(page.locator('[data-testid="screener-row"] td[data-col="p_long"]')).toHaveCount(0)
    await page.getByTestId('reset-filters').click()
    await expect(page.getByTestId('screener-row')).toHaveCount(api.rows.length)
  })

  test('сортировка по колонке', async ({ page }) => {
    await page.getByTestId('sort-rsi').click() // числовые — сначала по убыванию
    const vals = (await page.locator('[data-testid="screener-row"] td[data-col="rsi"]').allTextContents()).map((t) => num(t)).filter((v): v is number => v !== null)
    expect(vals.length).toBeGreaterThan(10)
    for (let i = 1; i < vals.length; i++) expect(vals[i]!).toBeLessThanOrEqual(vals[i - 1]!)
    await page.getByTestId('sort-rsi').click()
    const asc = (await page.locator('[data-testid="screener-row"] td[data-col="rsi"]').allTextContents()).map((t) => num(t)).filter((v): v is number => v !== null)
    for (let i = 1; i < asc.length; i++) expect(asc[i]!).toBeGreaterThanOrEqual(asc[i - 1]!)
  })

  test('пресет фильтров сохраняется в localStorage', async ({ page }) => {
    await page.getByTestId('class-crypto').click()
    await page.getByTestId('only-signals').check()
    await page.getByTestId('preset-new').click()
    await page.getByTestId('preset-name').fill('Крипто-сигналы')
    await page.getByTestId('preset-save').click()
    await page.getByTestId('reset-filters').click()
    await page.reload()
    await expect(page.getByTestId('screener-row').first()).toBeVisible()
    await page.getByTestId('preset-select').selectOption('Крипто-сигналы')
    await expect(page.getByTestId('only-signals')).toBeChecked()
    await expect(page.getByTestId('class-crypto')).toHaveAttribute('aria-pressed', 'true')
  })

  test('шапка: запас счёта из API, отсчёт до ресета, UTC/локальное, тема', async ({ page }) => {
    const accounts = await apiGet<{ id: number; daily_room_usd: number; total_room_usd: number }[]>(page, '/api/accounts')
    expectDisplayed(await page.getByTestId('hdr-daily-room').innerText(), accounts[0]!.daily_room_usd, 1, 'daily room')
    expectDisplayed(await page.getByTestId('hdr-total-room').innerText(), accounts[0]!.total_room_usd, 1, 'total room')
    await expect(page.getByTestId('reset-countdown')).toContainText(/до ресета|окно сброса/)

    await expect(page.getByTestId('tz-toggle')).toHaveText('UTC')
    await page.getByTestId('tz-toggle').click()
    await expect(page.getByTestId('tz-toggle')).toHaveText('UTC+4') // timezoneId: Asia/Dubai
    await page.reload()
    await expect(page.getByTestId('tz-toggle')).toHaveText('UTC+4')

    await page.getByTestId('theme-toggle').click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'light')
    await page.getByTestId('theme-toggle').click()
    await expect(page.locator('html')).toHaveAttribute('data-theme', 'dark')
    const bg = await page.evaluate(() => getComputedStyle(document.body).backgroundColor)
    expect(bg).toBe('rgb(11, 15, 22)')
  })

  test('калибровка устарела (meta.calibration_stale): баннер, вероятности серым, числа прежние', async ({ page }) => {
    await page.addInitScript(() => {
      window.__HH_MOCK_STALE__ = true
    })
    await page.reload()
    await expect(page.getByTestId('stale-banner')).toBeVisible()
    const meta = await apiGet<{ calibration_stale: boolean }>(page, '/api/meta')
    expect(meta.calibration_stale).toBe(true)
    const ui = await readTable(page)
    const kinds = await page.locator('[data-testid="screener-row"] td[data-col="p_long"] [data-kind]').evaluateAll((els) =>
      els.map((e) => e.getAttribute('data-kind')),
    )
    expect(kinds.every((k) => k === 'gray')).toBe(true)
    expect(ui.some((u) => u.cells.p_long?.gray === 'stale')).toBe(true)
    const api = await apiGet<ScreenerResp>(page, '/api/screener?tf=1h')
    compareRows(ui, api.rows)
  })

  test('WS: индикатор связи и переподключение', async ({ page }) => {
    await expect(page.getByTestId('ws-status')).toHaveAttribute('data-state', 'open')
    await page.evaluate(() => window.__hhMock!.dropWs())
    await expect(page.getByTestId('ws-status')).toHaveAttribute('data-state', 'reconnecting')
    await expect(page.getByTestId('ws-status')).toHaveAttribute('data-state', 'open', { timeout: 5000 })
  })

  test('live-обновления приходят по WS', async ({ page }) => {
    await page.evaluate(() => window.__hhMock!.freeze(false))
    const before = await page.getByTestId('screener-meta').innerText()
    await expect(page.getByTestId('screener-meta')).not.toHaveText(before, { timeout: 6000 })
  })
})
