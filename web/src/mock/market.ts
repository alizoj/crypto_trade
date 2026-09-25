// Генерация рыночных рядов, индикаторов, строк скринера и разбора сетапа для mock-режима.
import type {
  AnalysisResponse,
  Bar,
  BarsResponse,
  ChartMarker,
  Factor,
  ProbInfo,
  ScreenerRow,
  ScreenerStatus,
  Side,
  SideAnalysis,
  Timeframe,
  Trend,
} from '@/api/types'
import {
  HALTED,
  LOW_DATA,
  MOCK_SPECS,
  OFF_SESSION_UNDERLYING,
  UNCALIBRATED_CLASSES,
  groupOf,
  type MockInstrumentSpec,
} from './instruments'
import { adx, atr, avwap, cvd, ema, hashSeed, normal, pick, rng, round, roundPrice, rsi, series, volumeProfile, vwapDaily, wilson } from './math'

export const TF_SEC: Record<Timeframe, number> = { '15m': 900, '1h': 3600, '4h': 14400 }
export const TFS: Timeframe[] = ['15m', '1h', '4h']
export const BARS_TOTAL = 800
export const RR1 = 1.5
export const RR2 = 2.8
export const FEES_R = 0.06
export const MIN_PROB = 0.55
export const MIN_EV = 0.15

export interface SeriesState {
  spec: MockInstrumentSpec
  tf: Timeframe
  bars: Bar[]
  ema20: number[]
  ema50: number[]
  ema200: number[]
  vwap: number[]
  rsi: number[]
  atr: number[]
  cvd: number[]
  avwapAnchor: number
  avwap: (number | null)[]
}

export interface RowExtras {
  factors: { long: Factor[]; short: Factor[] }
  structure: string
  sweep: string
  divergence: string
}

const seriesCache = new Map<string, SeriesState>()

function volumeBase(spec: MockInstrumentSpec): number {
  // условный объём в базовом активе на час
  const usdPerHour = spec.cls === 'crypto' ? (spec.perLot >= 100000 ? 4e7 : 3e6) : spec.cls === 'metal' ? 2e7 : 5e6
  return usdPerHour / spec.price
}

function recompute(s: SeriesState): void {
  const closes = s.bars.map((b) => b.c)
  s.ema20 = ema(closes, 20)
  s.ema50 = ema(closes, 50)
  s.ema200 = ema(closes, 200)
  s.vwap = vwapDaily(s.bars)
  s.rsi = rsi(closes, 14)
  s.atr = atr(s.bars, 14)
  s.cvd = cvd(s.bars)
  s.avwap = avwap(s.bars, s.avwapAnchor)
}

export function nowSec(): number {
  return Math.floor(Date.now() / 1000)
}

function generateSeries(spec: MockInstrumentSpec, tf: Timeframe): SeriesState {
  const r = rng(hashSeed(`${spec.sym}|${tf}|bars`))
  const tfSec = TF_SEC[tf]
  const barVol = spec.vol * Math.sqrt(tfSec / 86400)
  const n = BARS_TOTAL
  const lastT = Math.floor(nowSec() / tfSec) * tfSec
  // доходности с режимами тренда
  const rets: number[] = []
  let drift = 0
  for (let i = 0; i < n; i++) {
    if (i % 90 === 0) drift = (r() - 0.5) * barVol * 0.16
    rets.push(drift + barVol * normal(r) * 0.9)
  }
  // строим закрытия назад от текущей цены
  const closes = new Array<number>(n)
  closes[n - 1] = spec.price
  for (let i = n - 1; i > 0; i--) closes[i - 1] = closes[i]! / Math.exp(rets[i]!)
  const vb = volumeBase(spec) * (tfSec / 3600)
  const bars: Bar[] = []
  for (let i = 0; i < n; i++) {
    const c = closes[i]!
    const o = i === 0 ? c / Math.exp(rets[0]!) : closes[i - 1]!
    const wick = barVol * 0.6
    const h = Math.max(o, c) * (1 + Math.abs(normal(r)) * wick * 0.5)
    const l = Math.min(o, c) * (1 - Math.abs(normal(r)) * wick * 0.5)
    const v = vb * Math.exp(normal(r) * 0.35) * (1 + (Math.abs(rets[i]!) / barVol) * 0.6)
    bars.push({ t: lastT - (n - 1 - i) * tfSec, o: roundPrice(o), h: roundPrice(h), l: roundPrice(l), c: roundPrice(c), v: round(v, v > 100 ? 0 : 3) })
  }
  // якорь AVWAP — минимум/максимум последних 150 баров
  let anchor = n - 150
  for (let i = n - 150; i < n - 5; i++) if (bars[i]!.l < bars[anchor]!.l) anchor = i
  const s: SeriesState = { spec, tf, bars, ema20: [], ema50: [], ema200: [], vwap: [], rsi: [], atr: [], cvd: [], avwapAnchor: anchor, avwap: [] }
  recompute(s)
  return s
}

export function getSeries(sym: string, tf: Timeframe): SeriesState | null {
  const key = `${sym}|${tf}`
  let s = seriesCache.get(key)
  if (!s) {
    const spec = MOCK_SPECS.find((x) => x.sym === sym)
    if (!spec) return null
    s = generateSeries(spec, tf)
    seriesCache.set(key, s)
  }
  return s
}

function last<T>(arr: T[]): T {
  return arr[arr.length - 1]!
}

function trendOf(s: SeriesState): Trend {
  const e50 = last(s.ema50)
  const e200 = last(s.ema200)
  const d = (e50 - e200) / e200
  if (Math.abs(d) < 0.002) return 0
  return d > 0 ? 1 : -1
}

function htfTrend(sym: string, tf: Timeframe): Trend {
  const higher: Timeframe = tf === '15m' ? '1h' : '4h'
  const s = getSeries(sym, higher)
  return s ? trendOf(s) : 0
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, v))
}

function f(name: string, label: string, value: Factor['value'], points: number, max: number): Factor {
  const p = Math.round(clamp(points, 0, max))
  const verdict: Factor['verdict'] = p >= max * 0.6 ? 'pro' : p <= max * 0.25 ? 'con' : 'neutral'
  return { name, label, value, points: p, max_points: max, verdict }
}

const TREND_RU: Record<number, string> = { 1: 'вверх', 0: 'флэт', [-1]: 'вниз' }

export interface RowCore {
  row: ScreenerRow
  extras: RowExtras
}

const rowCache = new Map<string, RowCore>()

function fundingFor(spec: MockInstrumentSpec): number {
  const r = rng(hashSeed(`${spec.sym}|funding`))
  if (spec.cls !== 'crypto') return round((r() - 0.4) * 0.0002, 6)
  return round(0.0001 + (r() - 0.45) * 0.0006, 6)
}

function oiChangeFor(spec: MockInstrumentSpec): number {
  const r = rng(hashSeed(`${spec.sym}|oi`))
  return round((r() - 0.42) * 30, 2)
}

export function change24h(sym: string): number | null {
  const s = getSeries(sym, '1h')
  if (!s) return null
  const c = last(s.bars).c
  const prev = s.bars[s.bars.length - 25]!.c
  return round((c / prev - 1) * 100, 2)
}

function buildFactors(s: SeriesState, htf: Trend, cvdTrend: Trend, funding: number, adxV: number, r: () => number): RowExtras {
  const rsiV = last(s.rsi)
  const price = last(s.bars).c
  const vwapV = last(s.vwap)
  const dist = ((price - vwapV) / vwapV) * 100
  const atrPct = (last(s.atr) / price) * 100
  const structure = pick(r, ['BOS вверх', 'BOS вниз', 'CHoCH вверх', 'CHoCH вниз', 'нет'] as const)
  const sweep = pick(r, ['снят минимум', 'снят максимум', 'нет', 'нет'] as const)
  const divergence = pick(r, ['бычья скрытая', 'медвежья скрытая', 'бычья', 'медвежья', 'нет', 'нет'] as const)
  const vols = s.bars.slice(-21, -1).map((b) => b.v)
  const relVol = last(s.bars).v / (vols.reduce((a, b) => a + b, 0) / vols.length)
  const lastUp = last(s.bars).c >= last(s.bars).o

  const side = (sd: Side): Factor[] => {
    const dir = sd === 'long' ? 1 : -1
    const trendPts = htf === dir ? 20 : htf === 0 ? 8 : 0
    const structPts = structure === 'нет' ? 5 : structure.endsWith(sd === 'long' ? 'вверх' : 'вниз') ? (structure.startsWith('BOS') ? 15 : 12) : 0
    const sweepPts = sweep === 'нет' ? 3 : (sweep === 'снят минимум') === (sd === 'long') ? 10 : 0
    const rsiPts = sd === 'long' ? ((70 - rsiV) / 40) * 10 : ((rsiV - 30) / 40) * 10
    const divPts = divergence === 'нет' ? 3 : divergence.startsWith(sd === 'long' ? 'бычья' : 'медвежья') ? 10 : 0
    const volPts = relVol >= 1.3 ? (lastUp === (sd === 'long') ? 10 : 2) : 5
    const vwapPts = sd === 'long' ? (dist >= -0.3 && dist <= atrPct * 1.2 ? 10 : dist < -0.3 ? 4 : 2) : dist <= 0.3 && dist >= -atrPct * 1.2 ? 10 : dist > 0.3 ? 4 : 2
    const cvdPts = cvdTrend === dir ? 10 : cvdTrend === 0 ? 5 : 0
    const fundPts = sd === 'long' ? (funding > 0.0003 ? 0 : funding < 0 ? 5 : 3) : funding > 0.0003 ? 5 : funding < -0.0001 ? 0 : 3
    const adxNote = adxV < 20 ? 'тренд слабый' : 'тренд есть'
    return [
      f('htf_trend', 'HTF-тренд (EMA50/200 старшего ТФ)', TREND_RU[htf] ?? 'флэт', trendPts, 20),
      f('structure', 'Структура (BOS/CHoCH)', structure, structPts, 15),
      f('sweep', 'Снятие ликвидности', sweep, sweepPts, 10),
      f('rsi', 'RSI(14)', round(rsiV, 1), rsiPts, 10),
      f('rsi_div', 'RSI-дивергенция', divergence, divPts, 10),
      f('volume', 'Объём (отн. к SMA20)', round(relVol, 2), volPts, 10),
      f('vwap', 'Положение к VWAP, %', round(dist, 2), vwapPts, 10),
      f('cvd', 'CVD-тренд', TREND_RU[cvdTrend] ?? 'флэт', cvdPts, 10),
      f('funding', 'Funding', `${round(funding * 100, 4)}%`, fundPts, 5),
      { name: 'adx', label: `ADX(14) — ${adxNote}`, value: round(adxV, 1), points: 0, max_points: 0, verdict: adxV < 20 ? 'con' : 'neutral' },
    ]
  }
  return { factors: { long: side('long'), short: side('short') }, structure, sweep, divergence }
}

function probFor(spec: MockInstrumentSpec, tf: Timeframe, side: Side, score: number): ProbInfo {
  const r = rng(hashSeed(`${spec.sym}|${tf}|${side}|prob`))
  const group = groupOf(spec)
  const offset = group === 'crypto_major' ? 0.02 : group === 'crypto_alt' ? -0.01 : -0.02
  const p = round(clamp(0.22 + 0.5 * (score / 100) + offset + (r() - 0.5) * 0.03, 0.05, 0.95), 4)
  let n: number
  if (LOW_DATA.has(spec.sym)) n = 20 + Math.floor(r() * 70)
  else if (group === 'crypto_major') n = 250 + Math.floor(r() * 700)
  else if (group === 'crypto_alt') n = 110 + Math.floor(r() * 300)
  else n = 100 + Math.floor(r() * 120)
  const [lo, hi] = wilson(p, n)
  return {
    p,
    ci_low: round(lo, 4),
    ci_high: round(hi, 4),
    n,
    calibrated: !UNCALIBRATED_CLASSES.has(spec.cls),
    low_data: n < 100,
  }
}

export function evFor(p: number): number {
  return round(p * RR1 - (1 - p) - FEES_R, 3)
}

export function sessionOpen(spec: MockInstrumentSpec): boolean {
  return !OFF_SESSION_UNDERLYING.has(spec.underlying)
}

function buildRow(spec: MockInstrumentSpec, tf: Timeframe): RowCore {
  const s = getSeries(spec.sym, tf)!
  const r = rng(hashSeed(`${spec.sym}|${tf}|row`))
  const price = last(s.bars).c
  const htf = htfTrend(spec.sym, tf)
  const cvdNow = last(s.cvd)
  const cvdPrev = s.cvd[s.cvd.length - 21]!
  const cvdScale = Math.abs(cvdNow) + Math.abs(cvdPrev) || 1
  const cvdTrend: Trend = Math.abs(cvdNow - cvdPrev) / cvdScale < 0.02 ? 0 : cvdNow > cvdPrev ? 1 : -1
  const funding = fundingFor(spec)
  const adxV = adx(s.bars, 14)
  const extras = buildFactors(s, htf, cvdTrend, funding, adxV, r)
  const scoreL = extras.factors.long.reduce((a, x) => a + x.points, 0)
  const scoreS = extras.factors.short.reduce((a, x) => a + x.points, 0)
  const pL = probFor(spec, tf, 'long', scoreL)
  const pS = probFor(spec, tf, 'short', scoreS)
  const open = sessionOpen(spec)
  const halted = HALTED.has(spec.sym)
  const row: ScreenerRow = {
    hh_symbol: spec.sym,
    base: spec.base,
    asset_class: spec.cls,
    group: groupOf(spec),
    price,
    change_24h_pct: change24h(spec.sym),
    htf_trend: htf,
    rsi14: round(last(s.rsi), 2),
    adx: round(adxV, 2),
    vwap_dist_pct: round(((price - last(s.vwap)) / last(s.vwap)) * 100, 3),
    atr_pct: round((last(s.atr) / price) * 100, 3),
    funding_rate: funding,
    oi_change_24h_pct: oiChangeFor(spec),
    cvd_trend: cvdTrend,
    score_long: scoreL,
    score_short: scoreS,
    p_long: pL,
    p_short: pS,
    ev_long_r: evFor(pL.p),
    ev_short_r: evFor(pS.p),
    status: 'none',
    active_signal_id: null,
    active_signal_side: null,
    max_per_lot_usd: spec.perLot,
    max_total_usd: spec.total,
    session_open: open,
    data_age_sec: halted ? 5400 + Math.floor(r() * 600) : 1 + Math.floor(r() * 25),
  }
  let status: ScreenerStatus = 'none'
  if (halted) status = 'halted'
  else if (!open) status = 'off_session'
  else {
    const best = Math.max(eligibleP(pL) ?? 0, eligibleP(pS) ?? 0)
    if (best >= 0.5) status = 'watch'
  }
  row.status = status
  return { row, extras }
}

function eligibleP(p: ProbInfo | null): number | null {
  return p && p.calibrated && !p.low_data ? p.p : null
}

/** Лучшая сторона для сигнала (если P и EV проходят пороги 8.2). */
export function signalSideFor(row: ScreenerRow): Side | null {
  if (row.status === 'halted' || row.status === 'off_session') return null
  const cands: [Side, number | null, number | null][] = [
    ['long', eligibleP(row.p_long), row.ev_long_r],
    ['short', eligibleP(row.p_short), row.ev_short_r],
  ]
  let best: Side | null = null
  let bestP = 0
  for (const [side, p, ev] of cands) {
    if (p !== null && ev !== null && p >= MIN_PROB && ev >= MIN_EV && p > bestP) {
      best = side
      bestP = p
    }
  }
  return best
}

export function getRowCore(sym: string, tf: Timeframe): RowCore | null {
  const key = `${sym}|${tf}`
  let rc = rowCache.get(key)
  if (!rc) {
    const spec = MOCK_SPECS.find((x) => x.sym === sym)
    if (!spec) return null
    rc = buildRow(spec, tf)
    rowCache.set(key, rc)
  }
  return rc
}

export function allRows(tf: Timeframe): ScreenerRow[] {
  return MOCK_SPECS.map((s) => getRowCore(s.sym, tf)!.row)
}

/**
 * Если после расчёта сигналов в ТФ меньше `min`, дотягиваем P у лучших подходящих строк
 * (чтобы в моке всегда были активные сигналы).
 */
export function ensureSignals(tf: Timeframe, min: number): void {
  const rows = allRows(tf)
  const have = rows.filter((r) => signalSideFor(r) !== null).length
  if (have >= min) return
  const cands = rows
    .filter((r) => r.status === 'watch' || r.status === 'none')
    .filter((r) => signalSideFor(r) === null)
    .map((r) => {
      const side: Side = (r.score_long ?? 0) >= (r.score_short ?? 0) ? 'long' : 'short'
      return { r, side, p: side === 'long' ? r.p_long : r.p_short }
    })
    .filter((c) => c.p && c.p.calibrated && !c.p.low_data)
    .sort((a, b) => (b.p!.p ?? 0) - (a.p!.p ?? 0))
  for (const c of cands.slice(0, min - have)) {
    const p = c.p!
    const bumped = round(0.56 + (hashSeed(c.r.hh_symbol + tf) % 80) / 1000, 4)
    const [lo, hi] = wilson(bumped, p.n)
    const np: ProbInfo = { ...p, p: bumped, ci_low: round(lo, 4), ci_high: round(hi, 4) }
    if (c.side === 'long') {
      c.r.p_long = np
      c.r.ev_long_r = evFor(bumped)
    } else {
      c.r.p_short = np
      c.r.ev_short_r = evFor(bumped)
    }
  }
}

// ---------- live-тики ----------

/** Небольшое движение цены последнего бара; возвращает обновлённые строки всех ТФ. */
export function tick(sym: string, r: () => number): void {
  const spec = MOCK_SPECS.find((x) => x.sym === sym)
  if (!spec || HALTED.has(sym)) return
  const move = normal(r) * spec.vol * 0.004
  let newPrice: number | null = null
  for (const tf of TFS) {
    const s = getSeries(sym, tf)!
    const b = last(s.bars)
    const c: number = newPrice ?? roundPrice(b.c * Math.exp(move))
    newPrice = c
    b.c = c
    b.h = Math.max(b.h, c)
    b.l = Math.min(b.l, c)
    b.v = round(b.v * 1.002, b.v > 100 ? 0 : 3)
    recompute(s)
  }
  const ch = change24h(sym)
  for (const tf of TFS) {
    const rc = rowCache.get(`${sym}|${tf}`)
    if (!rc) continue
    const s = getSeries(sym, tf)!
    const price = last(s.bars).c
    // строки — новые объекты (как пришли бы из API)
    rc.row = {
      ...rc.row,
      price,
      change_24h_pct: ch,
      rsi14: round(last(s.rsi), 2),
      vwap_dist_pct: round(((price - last(s.vwap)) / last(s.vwap)) * 100, 3),
      atr_pct: round((last(s.atr) / price) * 100, 3),
      data_age_sec: 1 + Math.floor(r() * 5),
    }
  }
}

export function setRowStatus(sym: string, tf: Timeframe, status: ScreenerStatus, signalId: number | null, side: Side | null): void {
  const rc = getRowCore(sym, tf)
  if (!rc) return
  rc.row = { ...rc.row, status, active_signal_id: signalId, active_signal_side: signalId === null ? null : side }
}

// ---------- бары для графика ----------

export function barsResponse(sym: string, tf: Timeframe, limit: number, markers: ChartMarker[], activeSignal: BarsResponse['active_signal']): BarsResponse | null {
  const s = getSeries(sym, tf)
  if (!s) return null
  const n = Math.max(1, Math.min(limit, s.bars.length))
  const from = s.bars.length - n
  const bars = s.bars.slice(from)
  const times = bars.map((b) => b.t)
  const sl = <T>(a: T[]) => a.slice(from)
  const vp = volumeProfile(s.bars.slice(-200), 40)
  const t0 = times[0] ?? 0
  return {
    tf,
    bars: bars.map((b) => ({ ...b })),
    indicators: {
      ema20: series(times, sl(s.ema20)),
      ema50: series(times, sl(s.ema50)),
      ema200: series(times, sl(s.ema200)),
      vwap: series(times, sl(s.vwap)),
      avwap: series(times, sl(s.avwap)),
      rsi: series(times, sl(s.rsi), 2),
      cvd: series(times, sl(s.cvd), 2),
      volume: bars.map((b) => ({ t: b.t, v: b.v, up: b.c >= b.o })),
    },
    volume_profile: vp,
    markers: markers.filter((m) => m.t >= t0).sort((a, b) => a.t - b.t),
    active_signal: activeSignal,
  }
}

// ---------- разбор ----------

export function analysisText(row: ScreenerRow, extras: RowExtras, tf: Timeframe): string[] {
  const t: string[] = []
  const tr = row.htf_trend === 1 ? 'восходящий' : row.htf_trend === -1 ? 'нисходящий' : 'боковой'
  t.push(`Тренд старшего ТФ ${tr}: EMA50 ${row.htf_trend === 1 ? 'выше' : row.htf_trend === -1 ? 'ниже' : 'около'} EMA200.`)
  if (row.rsi14 !== null) {
    const zone = row.rsi14 >= 70 ? 'в зоне перекупленности' : row.rsi14 <= 30 ? 'в зоне перепроданности' : 'в нейтральной зоне'
    t.push(`RSI(14) на ${tf} = ${row.rsi14.toFixed(1)} — ${zone}.`)
  }
  if (row.vwap_dist_pct !== null) {
    t.push(`Цена ${row.vwap_dist_pct >= 0 ? 'выше' : 'ниже'} дневного VWAP на ${Math.abs(row.vwap_dist_pct).toFixed(2)}%, ATR ${row.atr_pct?.toFixed(2) ?? '—'}% от цены.`)
  }
  if (extras.structure !== 'нет') t.push(`Структура: ${extras.structure}${extras.sweep !== 'нет' ? `, перед этим ${extras.sweep}` : ''}.`)
  if (extras.divergence !== 'нет') t.push(`На RSI ${extras.divergence} дивергенция.`)
  const adxV = row.adx ?? 0
  t.push(adxV < 20 ? `ADX ${adxV.toFixed(1)} — тренд слабый, пробои менее надёжны.` : `ADX ${adxV.toFixed(1)} — тренд выражен.`)
  return t.slice(0, 6)
}

export function setupStats(sym: string, tf: Timeframe, side: Side, prob: ProbInfo | null): SideAnalysis['setup_stats'] {
  const r = rng(hashSeed(`${sym}|${tf}|${side}|stats`))
  const n = prob?.n ?? 40 + Math.floor(r() * 60)
  const wr = prob ? clamp(prob.p + (r() - 0.5) * 0.04, 0.05, 0.95) : 0.3 + r() * 0.3
  const [lo, hi] = wilson(wr, n)
  const last10: ('tp' | 'sl' | 'timeout')[] = []
  for (let i = 0; i < 10; i++) {
    const x = r()
    last10.push(x < wr ? 'tp' : x < wr + 0.08 ? 'timeout' : 'sl')
  }
  return {
    n,
    winrate: round(wr, 4),
    avg_r: round(wr * RR1 - (1 - wr) - FEES_R + (r() - 0.5) * 0.05, 3),
    ci_low: round(lo, 4),
    ci_high: round(hi, 4),
    last10,
  }
}

export function contextFor(spec: MockInstrumentSpec, tf: Timeframe): AnalysisResponse['context'] {
  const r = rng(hashSeed(`${spec.sym}|ctx`))
  const now = nowSec()
  const funding: { t: number; v: number }[] = []
  const base = fundingFor(spec)
  for (let i = 29; i >= 0; i--) {
    funding.push({ t: Math.floor(now / 28800) * 28800 - i * 28800, v: round(base + (r() - 0.5) * 0.0002, 6) })
  }
  const oi: { t: number; v: number }[] = []
  let oiV = (spec.perLot * 400) / 1e6
  for (let i = 47; i >= 0; i--) {
    oiV *= 1 + (r() - 0.48) * 0.02
    oi.push({ t: Math.floor(now / 3600) * 3600 - i * 3600, v: round(oiV * 1e6, 0) })
  }
  const group = groupOf(spec)
  const btc = group === 'crypto_alt' || (group === 'crypto_major' && spec.sym !== 'BTCUSDT') ? getRowCore('BTCUSDT', tf)?.row.htf_trend ?? null : null
  let session: AnalysisResponse['context']['session'] = null
  if (group === 'tradfi') {
    const open = sessionOpen(spec)
    const next = new Date()
    next.setUTCHours(open ? 21 : 13, open ? 0 : 30, 0, 0)
    if (next.getTime() < Date.now()) next.setUTCDate(next.getUTCDate() + 1)
    session = spec.underlying === 'PREMARKET' ? { open: true, next_change: null } : { open, next_change: next.toISOString().replace(/\.\d{3}Z$/, 'Z') }
  }
  return { funding, oi, btc_trend: btc, session }
}
