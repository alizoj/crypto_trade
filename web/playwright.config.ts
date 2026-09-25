import { existsSync } from 'node:fs'
import { chromium, defineConfig, devices } from '@playwright/test'

// E2E против mock-режима (VITE_MOCK=1): API и WebSocket эмулируются в браузере.
const PORT = Number(process.env.E2E_PORT ?? 5199)
const BASE_URL = process.env.E2E_BASE_URL ?? `http://localhost:${PORT}`

// Если браузер Playwright не скачан (нет сети до CDN), используем установленный Chrome/Edge.
// Явно: PW_CHANNEL=chrome | msedge | chromium.
function pickChannel(): string | undefined {
  if (process.env.PW_CHANNEL) return process.env.PW_CHANNEL === 'chromium' ? undefined : process.env.PW_CHANNEL
  try {
    if (existsSync(chromium.executablePath())) return undefined
  } catch {
    /* нет сведений о пути — пробуем системный Chrome */
  }
  return 'chrome'
}
const channel = pickChannel()

export default defineConfig({
  testDir: './e2e',
  timeout: 45_000,
  expect: { timeout: 10_000 },
  fullyParallel: true,
  workers: process.env.CI ? 2 : 4,
  retries: process.env.CI ? 1 : 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: BASE_URL,
    locale: 'ru-RU',
    timezoneId: 'Asia/Dubai',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    channel,
  },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'], channel, viewport: { width: 1600, height: 1000 } }, testIgnore: /mobile\.spec/ },
    { name: 'mobile', use: { ...devices['Pixel 7'], channel, viewport: { width: 390, height: 844 } }, testMatch: /mobile\.spec/ },
  ],
  webServer: process.env.E2E_BASE_URL
    ? undefined
    : {
        command: `npx vite --mode mock --port ${PORT} --strictPort`,
        url: BASE_URL,
        reuseExistingServer: !process.env.CI,
        timeout: 120_000,
      },
})
