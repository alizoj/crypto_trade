import { test, expect } from '@playwright/test'
import { apiGet, expectDisplayed, freezeMock, loginOk, trackErrors } from './helpers'

interface Account {
  id: number
  name: string
  balance: number
  equity: number
  daily_room_usd: number
  total_room_usd: number
  daily_limit_usd: number
  total_limit_usd: number
  progress_pct: number | null
  trading_days: number
  min_trading_days: number
  target_lock: boolean
  positions: { id: number }[]
}

test.beforeEach(async ({ page }) => {
  await freezeMock(page)
  await loginOk(page)
})

test.describe('Счета', () => {
  test('значения карточек = API, target_lock подсвечен', async ({ page }) => {
    const errors = trackErrors(page)
    await page.getByRole('link', { name: 'Счета' }).click()
    const accs = await apiGet<Account[]>(page, '/api/accounts')
    expect(accs).toHaveLength(2)
    await expect(page.getByTestId('account-card')).toHaveCount(2)
    for (const a of accs) {
      const card = page.locator(`[data-testid="account-card"][data-account-id="${a.id}"]`)
      expectDisplayed(await card.locator('[data-field="balance"]').innerText(), a.balance, 1, 'balance')
      expectDisplayed(await card.locator('[data-field="equity"]').innerText(), a.equity, 1, 'equity')
      expectDisplayed(await card.locator('[data-field="daily-room"]').innerText(), a.daily_room_usd, 1, 'daily room')
      expectDisplayed(await card.locator('[data-field="total-room"]').innerText(), a.total_room_usd, 1, 'total room')
      expectDisplayed(await card.locator('[data-field="daily-limit"]').innerText(), a.daily_limit_usd, 1, 'daily limit')
      expectDisplayed(await card.locator('[data-field="total-limit"]').innerText(), a.total_limit_usd, 1, 'total limit')
      if (a.progress_pct !== null) expectDisplayed(await card.locator('[data-field="progress"]').innerText(), a.progress_pct, 1, 'progress')
      await expect(card.locator('[data-field="days"]')).toHaveText(`${a.trading_days}/${a.min_trading_days}`)
      await expect(card.getByTestId('position-row')).toHaveCount(a.positions.length)
      await expect(card.getByTestId('target-lock')).toHaveCount(a.target_lock ? 1 : 0)
    }
    expect(errors).toEqual([])
  })

  test('создание счёта, ручной ввод баланса, добавление и закрытие позиции', async ({ page }) => {
    await page.goto('/accounts')
    await page.getByTestId('new-account').click()
    await page.getByTestId('acc-name').fill('E2E 25k')
    await page.getByTestId('acc-type').selectOption('one_stage')
    await page.getByTestId('acc-size').selectOption('25000')
    await page.getByTestId('acc-submit').click()
    await expect(page.getByTestId('account-card')).toHaveCount(3)
    const card = page.getByTestId('account-card').filter({ hasText: 'E2E 25k' })
    await expect(card.locator('[data-field="balance"]')).toHaveText('$25 000.00')

    // PATCH баланса
    await card.getByTestId('edit-balance').click()
    await card.getByTestId('balance-input').fill('25400')
    await card.getByTestId('balance-save').click()
    await expect(card.locator('[data-field="balance"]')).toHaveText('$25 400.00')
    const id = Number(await card.getAttribute('data-account-id'))
    const acc = (await apiGet<Account[]>(page, '/api/accounts')).find((a) => a.id === id)!
    expect(acc.balance).toBe(25400)
    expectDisplayed(await card.locator('[data-field="daily-room"]').innerText(), acc.daily_room_usd, 1, 'daily room после PATCH')

    // позиция
    await card.getByTestId('add-position').click()
    await card.getByTestId('pos-symbol').selectOption('BTCUSDT')
    await card.getByTestId('pos-qty').fill('0.01')
    await card.getByTestId('pos-submit').click()
    await expect(card.getByTestId('position-row')).toHaveCount(1)
    await card.getByTestId('close-position').click()
    await card.getByTestId('exit-reason').selectOption('tp')
    await card.getByTestId('exit-note').fill('e2e: по плану')
    await card.getByRole('button', { name: 'Закрыть', exact: true }).click()
    await expect(card.getByTestId('position-row')).toHaveCount(0)

    // сделка ушла в журнал с причиной и заметкой
    await card.getByTestId('journal-link').click()
    await expect(page).toHaveURL(new RegExp(String.raw`/journal\?account=${id}$`))
    await expect(page.getByTestId('journal-row')).toHaveCount(1)
    await expect(page.getByTestId('journal-row').first()).toContainText('e2e: по плану')
    await expect(page.getByTestId('journal-reason').first()).toHaveValue('tp')
    await expect(page.locator('[data-field="series-progress"]')).toHaveText('1/20')
  })
})

test.describe('Журнал сделок', () => {
  test('серии по 20, итог = API, правка заметки и причины', async ({ page }) => {
    const errors = trackErrors(page)
    await page.getByRole('link', { name: 'Сделки' }).click()
    await expect(page).toHaveURL(/\/journal$/)
    const list = await apiGet<{ total: number; items: { id: number; pnl_usd: number }[] }>(page, '/api/journal?limit=100')
    const st = await apiGet<{ total: { n: number }; series: { n: number; complete: boolean }[] }>(page, '/api/journal/stats')
    await expect(page.getByTestId('journal-row')).toHaveCount(Math.min(100, list.total))
    await expect(page.locator('[data-field="total-n"]')).toHaveText(String(st.total.n))
    const cur = st.series.at(-1)!
    await expect(page.locator('[data-field="series-progress"]')).toHaveText(`${cur.n}/20`)
    await expect(page.getByTestId('series-row')).toHaveCount(st.series.length - 1)
    const first = page.getByTestId('journal-row').first()
    await expect(first).toHaveAttribute('data-trade-id', String(list.items[0]!.id))
    expectDisplayed(await first.locator('[data-field="pnl"]').innerText(), list.items[0]!.pnl_usd, 1, 'pnl')

    await first.getByTestId('note-edit').click()
    await first.getByTestId('note-input').fill('закрыл раньше из страха')
    await first.getByTestId('note-save').click()
    await expect(first).toContainText('закрыл раньше из страха')
    await first.getByTestId('journal-reason').selectOption('other')
    await expect
      .poll(async () => (await apiGet<{ items: { id: number; note: string; exit_reason: string }[] }>(page, '/api/journal?limit=1')).items[0])
      .toMatchObject({ note: 'закрыл раньше из страха', exit_reason: 'other' })

    // фильтр по счёту
    await page.getByTestId('jf-account').selectOption({ index: 2 })
    await expect(page).toHaveURL(/account=\d+/)
    expect(errors).toEqual([])
  })
})

test.describe('Журнал сигналов', () => {
  test('таблица, пагинация, «взял/пропустил», калибровка', async ({ page }) => {
    const errors = trackErrors(page)
    await page.getByRole('link', { name: 'Сигналы' }).click()
    const first = await apiGet<{ total: number; items: { id: number; outcome_r: number | null; status: string }[] }>(
      page,
      '/api/signals?limit=50&offset=0',
    )
    await expect(page.getByTestId('signal-row')).toHaveCount(50)
    await expect(page.getByTestId('signals-total')).toHaveText(String(first.total))
    // первая строка = первый элемент API, исход совпадает
    const firstRow = page.getByTestId('signal-row').first()
    await expect(firstRow).toHaveAttribute('data-signal-id', String(first.items[0]!.id))
    const withOutcome = first.items.find((s) => s.outcome_r !== null)!
    const oRow = page.locator(`[data-testid="signal-row"][data-signal-id="${withOutcome.id}"]`)
    expectDisplayed(await oRow.locator('[data-field="outcome"]').innerText(), withOutcome.outcome_r, 1, 'outcome')

    // решение
    await firstRow.getByTestId('took').click()
    await expect(firstRow.getByTestId('took')).toHaveAttribute('aria-pressed', 'true')
    await firstRow.getByTestId('skipped').click()
    await expect(firstRow.getByTestId('skipped')).toHaveAttribute('aria-pressed', 'true')
    await expect(firstRow.getByTestId('took')).toHaveAttribute('aria-pressed', 'false')

    // пагинация
    await page.getByTestId('page-next').click()
    await expect(page.getByTestId('signals-range')).toContainText('51–100')
    await expect(page).toHaveURL(/offset=50/)

    // фильтр по статусу
    await page.getByTestId('sf-status').selectOption('tp')
    const tp = await apiGet<{ total: number }>(page, '/api/signals?status=tp&limit=50&offset=0')
    await expect(page.getByTestId('signals-total')).toHaveText(String(tp.total))
    for (const t of await page.locator('[data-testid="signal-row"] [data-field="status"]').allTextContents()) expect(t.trim()).toBe('TP')

    // группа статусов уходит через запятую
    await page.getByTestId('sf-status').selectOption('closed')
    const closed = await apiGet<{ total: number }>(page, '/api/signals?status=tp,sl,timeout&limit=50&offset=0')
    await expect(page.getByTestId('signals-total')).toHaveText(String(closed.total))
    for (const t of await page.locator('[data-testid="signal-row"] [data-field="status"]').allTextContents()) {
      expect(['TP', 'SL', 'Таймаут']).toContain(t.trim())
    }

    // калибровка
    const cal = await apiGet<{ brier: number; n_total: number }>(page, '/api/calibration/live')
    await expect(page.getByTestId('calibration-chart').locator('circle')).not.toHaveCount(0)
    expectDisplayed(await page.getByTestId('brier').innerText(), cal.brier, 1, 'brier')
    await expect(page.getByTestId('calib-n')).toHaveText(String(cal.n_total))
    await page.getByLabel('Группа').selectOption('tradfi')
    await expect(page.getByTestId('calib-stale')).toBeVisible()
    expect(errors).toEqual([])
  })
})

test.describe('Бэктесты', () => {
  test('список, сравнение, HTML-отчёт, реестр испытаний', async ({ page }) => {
    const errors = trackErrors(page)
    await page.getByRole('link', { name: 'Бэктесты' }).click()
    const runs = await apiGet<{ id: number; metrics: Record<string, number> }[]>(page, '/api/backtests?limit=50')
    await expect(page.getByTestId('run-row')).toHaveCount(runs.length)
    for (const r of runs) {
      const row = page.locator(`[data-testid="run-row"][data-run-id="${r.id}"]`)
      expectDisplayed(await row.locator('[data-metric="p_pass_both"]').innerText(), r.metrics.p_pass_both!, 100, 'p_pass_both')
      expectDisplayed(await row.locator('[data-metric="dsr"]').innerText(), r.metrics.dsr!, 1, 'dsr')
    }
    // сравнение двух прогонов
    await page.getByTestId('run-compare').nth(0).check()
    await page.getByTestId('run-compare').nth(1).check()
    await expect(page.getByTestId('compare-table')).toBeVisible()
    await expect(page.locator('[data-testid="compare-table"] thead th')).toHaveCount(3)
    await expect(page.locator('[data-testid="compare-table"] [data-best="1"]').first()).toBeVisible()

    // отчёт
    await page.getByTestId('open-report').first().click()
    const frame = page.frameLocator('[data-testid="report-iframe"]')
    await expect(frame.locator('h1')).toContainText(`#${runs[0]!.id}`)
    await expect(page.getByRole('link', { name: /новой вкладке/ })).toHaveAttribute('href', /^blob:/)

    // реестр испытаний
    const trials = await apiGet<{ total: number; items: unknown[] }>(page, '/api/trials?limit=100')
    await expect(page.getByTestId('trial-row')).toHaveCount(trials.items.length)
    expect(errors).toEqual([])
  })
})
