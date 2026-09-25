import { test, expect } from '@playwright/test'
import { freezeMock, login, loginOk, trackErrors } from './helpers'

test.describe('Авторизация', () => {
  test.beforeEach(async ({ page }) => {
    await freezeMock(page)
  })

  test('без сессии любой экран уводит на /login с возвратом', async ({ page }) => {
    await page.goto('/accounts')
    await expect(page).toHaveURL(/\/login\?redirect=(%2F|\/)accounts/)
    await expect(page.getByTestId('login-form')).toBeVisible()
  })

  test('вход по паролю и возврат на исходный экран', async ({ page }) => {
    const errors = trackErrors(page)
    await page.goto('/signals')
    await expect(page).toHaveURL(/\/login/)
    await page.getByLabel('Имя пользователя').fill('trader')
    const submit = page.getByRole('button', { name: 'Войти' })
    // кнопка неактивна, пока не введён пароль
    await expect(submit).toBeDisabled()
    await page.getByLabel('Пароль').fill('secret')
    await submit.click()
    await expect(page).toHaveURL(/\/signals$/)
    await expect(page.getByTestId('signal-row').first()).toBeVisible()
    expect(errors).toEqual([])
  })

  test('401 от API во время работы → экран логина', async ({ page }) => {
    await loginOk(page)
    await page.evaluate(() => window.__hhMock!.logout()) // сессия истекла на «сервере»
    await page.getByRole('link', { name: 'Бэктесты' }).click()
    await expect(page).toHaveURL(/\/login/)
    await expect(page.getByTestId('login-form')).toBeVisible()
  })

  test('WS закрыт кодом 4401 → экран логина без действий пользователя', async ({ page }) => {
    await loginOk(page)
    await page.evaluate(() => {
      window.__hhMock!.logout() // сессия истекла на «сервере»
      window.__hhMock!.dropWs() // обрыв → переподключение → сервер закрывает кодом 4401
    })
    await expect(page).toHaveURL(/\/login/, { timeout: 8000 })
    await expect(page.getByTestId('login-form')).toBeVisible()
    // после повторного входа live-связь восстанавливается
    await page.getByLabel('Имя пользователя').fill('trader')
    await page.getByLabel('Пароль').fill('secret')
    await page.getByRole('button', { name: 'Войти' }).click()
    await expect(page.getByTestId('ws-status')).toHaveAttribute('data-state', 'open')
  })

  test('выход', async ({ page }) => {
    await login(page)
    await expect(page).toHaveURL(/\/$/)
    await page.getByRole('button', { name: 'Выйти' }).click()
    await expect(page).toHaveURL(/\/login/)
    await page.goto('/')
    await expect(page).toHaveURL(/\/login/)
  })
})
