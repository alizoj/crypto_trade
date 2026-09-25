// Инструменты для mock-режима — подмножество config/instruments.csv (лимиты HashHedge и даты оттуда),
// цены и волатильность — правдоподобные фиктивные значения.
import type { AssetClass, Group, Instrument } from '@/api/types'

export interface MockInstrumentSpec {
  sym: string
  base: string
  cls: AssetClass
  underlying: 'COIN' | 'EQUITY' | 'KR_EQUITY' | 'COMMODITY' | 'PREMARKET'
  perLot: number
  total: number
  start: string
  price: number
  /** дневная волатильность, доля */
  vol: number
  bybit: string | null
  notes: string
}

const S = (
  sym: string,
  cls: AssetClass,
  underlying: MockInstrumentSpec['underlying'],
  perLot: number,
  total: number,
  start: string,
  price: number,
  vol: number,
  bybit: string | null = sym,
  notes = '',
): MockInstrumentSpec => ({ sym, base: sym.replace(/USDT$/, ''), cls, underlying, perLot, total, start, price, vol, bybit, notes })

export const MOCK_SPECS: MockInstrumentSpec[] = [
  // crypto major (max_per_lot >= 100k)
  S('BTCUSDT', 'crypto', 'COIN', 250000, 500000, '2019-09-08T17:55:00Z', 112450, 0.028),
  S('ETHUSDT', 'crypto', 'COIN', 250000, 500000, '2019-11-27T07:45:00Z', 4381.5, 0.036),
  S('SOLUSDT', 'crypto', 'COIN', 250000, 500000, '2020-09-14T07:00:00Z', 212.43, 0.045),
  S('XRPUSDT', 'crypto', 'COIN', 250000, 500000, '2020-01-06T08:20:00Z', 2.9412, 0.042),
  S('DOGEUSDT', 'crypto', 'COIN', 250000, 500000, '2020-07-10T09:00:00Z', 0.23814, 0.055),
  S('BNBUSDT', 'crypto', 'COIN', 100000, 500000, '2020-02-10T08:00:00Z', 905.2, 0.03),
  S('LINKUSDT', 'crypto', 'COIN', 100000, 500000, '2020-01-17T08:00:00Z', 23.114, 0.05),
  S('ADAUSDT', 'crypto', 'COIN', 100000, 500000, '2020-01-31T08:00:00Z', 0.8423, 0.05),
  S('LTCUSDT', 'crypto', 'COIN', 150000, 500000, '2020-01-09T08:05:00Z', 112.54, 0.042),
  S('XLMUSDT', 'crypto', 'COIN', 100000, 500000, '2020-01-20T08:00:00Z', 0.38211, 0.048),
  S('AAVEUSDT', 'crypto', 'COIN', 100000, 250000, '2020-10-16T07:00:00Z', 298.41, 0.055),
  S('PEPEUSDT', 'crypto', 'COIN', 250000, 500000, '2023-05-05T00:00:00Z', 0.000010523, 0.07, '1000PEPEUSDT', 'Binance: 1000PEPEUSDT (mult 1000)'),
  S('SHIBUSDT', 'crypto', 'COIN', 250000, 500000, '2021-05-10T07:00:00Z', 0.000012981, 0.055, 'SHIB1000USDT', 'Binance: 1000SHIBUSDT, Bybit: SHIB1000USDT'),
  S('FLOKIUSDT', 'crypto', 'COIN', 100000, 500000, '2023-05-06T00:00:00Z', 0.00009851, 0.075, '1000FLOKIUSDT'),
  S('BONKUSDT', 'crypto', 'COIN', 100000, 500000, '2023-11-22T14:00:00Z', 0.00002315, 0.08, '1000BONKUSDT'),
  // crypto alt
  S('TRXUSDT', 'crypto', 'COIN', 50000, 100000, '2020-01-15T08:05:00Z', 0.34122, 0.025),
  S('MANAUSDT', 'crypto', 'COIN', 5000, 5000, '2021-03-14T07:00:00Z', 0.30214, 0.06),
  S('ARKMUSDT', 'crypto', 'COIN', 5000, 5000, '2023-07-28T02:45:00Z', 0.58321, 0.07),
  S('DYMUSDT', 'crypto', 'COIN', 5000, 5000, '2024-02-07T07:00:00Z', 0.27452, 0.08),
  S('1INCHUSDT', 'crypto', 'COIN', 1000, 1000, '2020-12-25T07:00:00Z', 0.26871, 0.06),
  S('JOEUSDT', 'crypto', 'COIN', 5000, 5000, '2023-03-28T00:00:00Z', 0.16552, 0.07, null, 'Bybit: контракт закрыт'),
  S('TRBUSDT', 'crypto', 'COIN', 5000, 5000, '2020-09-03T07:00:00Z', 42.812, 0.075),
  S('ORDIUSDT', 'crypto', 'COIN', 5000, 5000, '2023-11-07T12:30:00Z', 11.423, 0.075),
  S('WLFIUSDT', 'crypto', 'COIN', 5000, 10000, '2025-08-23T08:30:00Z', 0.22314, 0.09),
  S('UBUSDT', 'crypto', 'COIN', 5000, 20000, '2025-09-12T10:30:00Z', 0.081204, 0.1),
  S('PEOPLEUSDT', 'crypto', 'COIN', 5000, 5000, '2021-12-23T07:00:00Z', 0.021432, 0.08),
  S('ARUSDT', 'crypto', 'COIN', 5000, 5000, '2021-09-28T07:00:00Z', 7.8512, 0.065),
  S('BSVUSDT', 'crypto', 'COIN', 5000, 5000, '2023-10-20T12:30:00Z', 27.412, 0.05),
  S('XMRUSDT', 'crypto', 'COIN', 25000, 500000, '2020-02-03T08:00:00Z', 318.42, 0.04),
  // TradFi: металлы, энергия, акции, ETF
  S('XAUUSDT', 'metal', 'COMMODITY', 250000, 500000, '2025-12-11T08:05:00Z', 3712.4, 0.012),
  S('XAGUSDT', 'metal', 'COMMODITY', 250000, 500000, '2026-01-07T10:00:00Z', 43.214, 0.02),
  S('XPTUSDT', 'metal', 'COMMODITY', 20000, 75000, '2026-01-30T10:00:00Z', 1405.2, 0.022, null),
  S('COPPERUSDT', 'metal', 'COMMODITY', 100000, 400000, '2026-03-06T09:00:00Z', 4.6123, 0.016, null),
  S('CLUSDT', 'energy', 'COMMODITY', 250000, 500000, '2026-04-01T09:00:00Z', 64.31, 0.022),
  S('BZUSDT', 'energy', 'COMMODITY', 250000, 500000, '2026-04-01T09:10:00Z', 67.84, 0.021),
  S('NATGASUSDT', 'energy', 'COMMODITY', 50000, 200000, '2026-04-01T09:20:00Z', 2.9312, 0.035, null),
  S('TSLAUSDT', 'equity', 'EQUITY', 25000, 50000, '2026-01-28T14:30:00Z', 418.22, 0.032),
  S('AAPLUSDT', 'equity', 'EQUITY', 25000, 100000, '2026-04-06T13:50:00Z', 238.51, 0.016),
  S('MSTRUSDT', 'equity', 'EQUITY', 150000, 350000, '2026-02-09T14:30:00Z', 342.18, 0.045),
  S('COINUSDT', 'equity', 'EQUITY', 75000, 250000, '2026-02-09T15:00:00Z', 318.04, 0.038),
  S('METAUSDT', 'equity', 'EQUITY', 15000, 50000, '2026-03-26T14:30:00Z', 752.3, 0.02),
  S('QQQUSDT', 'etf', 'EQUITY', 250000, 500000, '2026-04-06T13:30:00Z', 591.24, 0.011),
  S('SPYUSDT', 'etf', 'EQUITY', 100000, 100000, '2026-04-06T13:40:00Z', 662.18, 0.009),
  S('EWJUSDT', 'etf', 'EQUITY', 30000, 120000, '2026-03-19T13:30:00Z', 77.412, 0.01),
  S('SAMSUNGUSDT', 'equity', 'KR_EQUITY', 40000, 125000, '2026-06-02T03:10:00Z', 58.214, 0.022),
  S('OPENAIUSDT', 'equity', 'PREMARKET', 7500, 25000, '2026-05-26T08:30:00Z', 312.4, 0.04, null, 'Bybit: PreLaunch'),
]

export function groupOf(spec: Pick<MockInstrumentSpec, 'cls' | 'perLot'>): Group {
  if (spec.cls !== 'crypto') return 'tradfi'
  return spec.perLot >= 100000 ? 'crypto_major' : 'crypto_alt'
}

export function toInstrument(s: MockInstrumentSpec): Instrument {
  return {
    hh_symbol: s.sym,
    base: s.base,
    asset_class: s.cls,
    group: groupOf(s),
    market: 'crypto_tradfi',
    underlying_type: s.underlying,
    max_per_lot_usd: s.perLot,
    max_total_usd: s.total,
    history_start: s.start,
    binance_um: s.notes.startsWith('Binance: 1000') ? `1000${s.sym}` : s.sym,
    bybit_linear: s.bybit,
    notes: s.notes,
  }
}

// Особые случаи для демонстрации серых состояний.
/** молодые листинги / мало сделок в корзине → low_data */
export const LOW_DATA = new Set(['WLFIUSDT', 'UBUSDT', 'DYMUSDT', 'XPTUSDT', 'NATGASUSDT', 'COPPERUSDT'])
/** нет откалиброванной модели для пула (TradFi без полной истории) → calibrated=false */
export const UNCALIBRATED_CLASSES = new Set<AssetClass>(['equity'])
/** торги приостановлены (замороженные бары) */
export const HALTED = new Set(['JOEUSDT'])
/** базовый рынок закрыт (фиксировано в моке, чтобы e2e были детерминированными) */
export const OFF_SESSION_UNDERLYING = new Set(['EQUITY', 'KR_EQUITY'])
