import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import type { Account, ScreenerRow, Signal } from '@/api/types'
import { readString, writeString, readJSON, STORAGE_KEYS } from '@/utils/storage'
import { usePrefsStore } from './prefs'
import { useAccountsStore, upsertAccount } from './accounts'
import { applyRowsMessage, useScreenerStore, type RowsState } from './screener'
import { useMetaStore } from './meta'

function row(sym: string, over: Partial<ScreenerRow> = {}): ScreenerRow {
  return {
    hh_symbol: sym,
    base: sym.replace('USDT', ''),
    asset_class: 'crypto',
    group: 'crypto_major',
    price: 100,
    change_24h_pct: 0,
    htf_trend: 0,
    rsi14: 50,
    adx: 20,
    vwap_dist_pct: 0,
    atr_pct: 1,
    funding_rate: 0,
    oi_change_24h_pct: 0,
    cvd_trend: 0,
    score_long: 50,
    score_short: 50,
    p_long: null,
    p_short: null,
    ev_long_r: null,
    ev_short_r: null,
    status: 'none',
    active_signal_id: null,
    active_signal_side: null,
    max_per_lot_usd: 1000,
    max_total_usd: 1000,
    session_open: true,
    data_age_sec: 1,
    ...over,
  }
}

function account(id: number, over: Partial<Account> = {}): Account {
  return {
    id,
    name: `A${id}`,
    challenge_type: 'two_stage',
    market: 'crypto_tradfi',
    size: 10000,
    stage: 'stage1',
    balance: 10000,
    equity: 10000,
    start_of_day_balance: 10000,
    daily_floor: 9500,
    total_floor: 9000,
    daily_limit_usd: 500,
    total_limit_usd: 1000,
    daily_room_usd: 500,
    total_room_usd: 1000,
    target_usd: 10800,
    progress_pct: 0,
    trading_days: 0,
    min_trading_days: 5,
    soft_stop_hit: false,
    target_lock: false,
    status: 'active',
    positions: [],
    updated_at: '2026-09-23T00:00:00Z',
    month: null,
    ...over,
  }
}

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
})
const spies: { mockRestore: () => void }[] = []
function blockStorage(getErr: () => Error, setErr: () => Error): void {
  spies.push(
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw getErr()
    }),
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw setErr()
    }),
  )
}
afterEach(() => {
  while (spies.length) spies.pop()!.mockRestore()
  vi.restoreAllMocks()
})

describe('localStorage может бросать', () => {
  it('safe-обёртки не пробрасывают исключения', () => {
    blockStorage(
      () => new DOMException('denied', 'SecurityError'),
      () => new DOMException('quota', 'QuotaExceededError'),
    )
    expect(readString('x')).toBeNull()
    expect(writeString('x', '1')).toBe(false)
    expect(readJSON('x', { a: 1 })).toEqual({ a: 1 })
  })

  it('сторы работают, даже если хранилище недоступно', () => {
    blockStorage(
      () => new Error('blocked'),
      () => new Error('blocked'),
    )
    const prefs = usePrefsStore()
    expect(prefs.theme).toBe('system')
    expect(() => prefs.cycleTheme()).not.toThrow()
    const s = useScreenerStore()
    expect(s.filters.side).toBe('both')
    expect(() => s.savePreset('p')).not.toThrow()
  })
})

describe('prefs', () => {
  it('тема: системная → светлая → тёмная → системная, сохраняется и ставит data-theme', async () => {
    const prefs = usePrefsStore()
    prefs.cycleTheme()
    await Promise.resolve()
    expect(prefs.theme).toBe('light')
    expect(localStorage.getItem(STORAGE_KEYS.theme)).toBe('light')
    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
    prefs.cycleTheme()
    await Promise.resolve()
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    prefs.cycleTheme()
    await Promise.resolve()
    expect(prefs.theme).toBe('system')
    expect(document.documentElement.hasAttribute('data-theme')).toBe(false)
    expect(localStorage.getItem(STORAGE_KEYS.theme)).toBeNull()
  })

  it('пояс времени читается из localStorage и переключается', async () => {
    localStorage.setItem(STORAGE_KEYS.tz, 'local')
    const prefs = usePrefsStore()
    expect(prefs.tz).toBe('local')
    prefs.toggleTz()
    await Promise.resolve()
    expect(localStorage.getItem(STORAGE_KEYS.tz)).toBe('utc')
  })
})

describe('accounts', () => {
  it('upsertAccount заменяет по id или добавляет', () => {
    const list = [account(1), account(2)]
    const next = upsertAccount(list, account(2, { balance: 11000 }))
    expect(next).not.toBe(list)
    expect(next[1]!.balance).toBe(11000)
    expect(upsertAccount(list, account(3))).toHaveLength(3)
  })

  it('выбранный счёт: сохранённый id, иначе первый активный', () => {
    const accs = useAccountsStore()
    accs.list = [account(1, { status: 'failed' }), account(2), account(3)]
    expect(accs.selected?.id).toBe(2)
    accs.select(3)
    expect(accs.selected?.id).toBe(3)
    accs.select(99)
    expect(accs.selected?.id).toBe(2)
  })
})

describe('screener', () => {
  const base: RowsState = { tf: '1h', rows: [row('BTCUSDT'), row('ETHUSDT')], updatedAt: 't0', ready: true }

  it('snapshot заменяет строки, update — только изменившиеся', () => {
    const snap = applyRowsMessage(base, { type: 'snapshot', tf: '1h', updated_at: 't1', rows: [row('SOLUSDT')] })!
    expect(snap.rows.map((r) => r.hh_symbol)).toEqual(['SOLUSDT'])
    const upd = applyRowsMessage(base, { type: 'update', tf: '1h', updated_at: 't2', rows: [row('ETHUSDT', { price: 200 }), row('NEWUSDT')] })!
    expect(upd.rows.map((r) => r.hh_symbol)).toEqual(['BTCUSDT', 'ETHUSDT', 'NEWUSDT'])
    expect(upd.rows[1]!.price).toBe(200)
    expect(upd.rows[0]).toBe(base.rows[0]) // неизменённая строка — тот же объект
    expect(upd.updatedAt).toBe('t2')
  })

  it('сообщения другого ТФ и ping игнорируются', () => {
    expect(applyRowsMessage(base, { type: 'update', tf: '4h', updated_at: 'x', rows: [row('BTCUSDT', { price: 1 })] })).toBeNull()
    expect(applyRowsMessage(base, { type: 'ping', ts: 'x' })).toBeNull()
  })

  it('handleMessage: account обновляет счета, signal запоминается, сторона — из active_signal_side', () => {
    const s = useScreenerStore()
    const accs = useAccountsStore()
    accs.list = [account(1)]
    s.handleMessage({
      type: 'snapshot',
      tf: s.tf,
      updated_at: 't',
      rows: [
        row('BTCUSDT', { status: 'signal', active_signal_id: 7, active_signal_side: 'short', score_long: 80, score_short: 20 }),
        row('ETHUSDT', { score_long: 80, score_short: 20 }),
      ],
    })
    expect(s.state.rows).toHaveLength(2)
    s.handleMessage({ type: 'account', account: account(1, { daily_room_usd: 123 }) })
    expect(accs.list[0]!.daily_room_usd).toBe(123)
    s.handleMessage({ type: 'signal', signal: { id: 8, side: 'long' } as Signal })
    expect(s.lastSignal?.id).toBe(8)

    // сторона сигнала важнее score
    s.setFilters({ ...s.filters, side: 'short' })
    expect(s.filteredRows.map((r) => r.hh_symbol)).toEqual(['BTCUSDT'])
    s.setFilters({ ...s.filters, side: 'long' })
    expect(s.filteredRows.map((r) => r.hh_symbol)).toEqual(['ETHUSDT'])
  })

  it('calibrationStale берётся из /api/meta.calibration_stale', () => {
    const s = useScreenerStore()
    const meta = useMetaStore()
    expect(s.calibrationStale).toBe(false)
    meta.meta = {
      server_time: '2026-09-23T12:00:00Z',
      reset_window: { start_utc: '20:00', end_utc: '20:10' },
      next_reset: '2026-09-23T20:00:00Z',
      calibrated: true,
      calibration_stale: true,
      timeframes: ['15m', '1h', '4h'],
      asset_classes: ['crypto'],
      groups: ['crypto_major'],
    }
    expect(s.calibrationStale).toBe(true)
  })

  it('фильтры и пресеты сохраняются в localStorage', async () => {
    const s = useScreenerStore()
    s.setFilters({ ...s.filters, onlySignals: true, classes: ['metal'] })
    s.savePreset('Металлы')
    await new Promise((r) => setTimeout(r, 0))
    const state = JSON.parse(localStorage.getItem(STORAGE_KEYS.screenerState)!)
    expect(state.filters.onlySignals).toBe(true)
    expect(JSON.parse(localStorage.getItem(STORAGE_KEYS.screenerPresets)!)[0].name).toBe('Металлы')

    setActivePinia(createPinia())
    const s2 = useScreenerStore()
    expect(s2.filters.classes).toEqual(['metal'])
    s2.resetFilters()
    expect(s2.filters.onlySignals).toBe(false)
    s2.applyPreset('Металлы')
    expect(s2.filters.onlySignals).toBe(true)
  })

  it('смена ТФ без WS загружает REST', async () => {
    const fetchMock = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ tf: '4h', updated_at: 'u', rows: [row('XAUUSDT')] }), { status: 200 }),
    )
    vi.stubGlobal('fetch', fetchMock)
    const s = useScreenerStore()
    s.setTf('4h')
    await vi.waitFor(() => expect(s.state.ready).toBe(true))
    expect(fetchMock.mock.calls[0]![0]).toBe('/api/screener?tf=4h')
    expect(s.state.rows[0]!.hh_symbol).toBe('XAUUSDT')
    vi.unstubAllGlobals()
  })
})
