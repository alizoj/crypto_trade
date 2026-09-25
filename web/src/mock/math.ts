// Детерминированный ГПСЧ и индикаторы для генерации фикстур mock-режима.
import type { Bar, TimeValue } from '@/api/types'

/** mulberry32 */
export function rng(seed: number): () => number {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

export function hashSeed(s: string): number {
  let h = 2166136261
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return h >>> 0
}

export function normal(r: () => number): number {
  const u = Math.max(1e-12, r())
  const v = r()
  return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v)
}

export function pick<T>(r: () => number, arr: readonly T[]): T {
  return arr[Math.floor(r() * arr.length) % arr.length]!
}

export function round(v: number, decimals: number): number {
  const k = Math.pow(10, decimals)
  return Math.round(v * k) / k
}

/** Округление цены до «разумного» шага по величине. */
export function roundPrice(v: number): number {
  const a = Math.abs(v)
  const d = a >= 10000 ? 1 : a >= 1000 ? 2 : a >= 10 ? 3 : a >= 1 ? 4 : a >= 0.1 ? 5 : a >= 0.01 ? 6 : Math.min(10, Math.ceil(-Math.log10(a)) + 3)
  return round(v, d)
}

export function ema(values: number[], period: number): number[] {
  const k = 2 / (period + 1)
  const out: number[] = []
  let prev = values[0] ?? 0
  for (let i = 0; i < values.length; i++) {
    const v = values[i]!
    prev = i === 0 ? v : v * k + prev * (1 - k)
    out.push(prev)
  }
  return out
}

/** RSI Уайлдера. Первые `period` значений — NaN. */
export function rsi(closes: number[], period = 14): number[] {
  const out: number[] = new Array(closes.length).fill(NaN)
  let gain = 0
  let loss = 0
  for (let i = 1; i < closes.length; i++) {
    const ch = closes[i]! - closes[i - 1]!
    const g = Math.max(0, ch)
    const l = Math.max(0, -ch)
    if (i <= period) {
      gain += g
      loss += l
      if (i === period) {
        gain /= period
        loss /= period
        out[i] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss)
      }
    } else {
      gain = (gain * (period - 1) + g) / period
      loss = (loss * (period - 1) + l) / period
      out[i] = loss === 0 ? 100 : 100 - 100 / (1 + gain / loss)
    }
  }
  return out
}

export function trueRange(bars: Bar[]): number[] {
  return bars.map((b, i) => {
    if (i === 0) return b.h - b.l
    const pc = bars[i - 1]!.c
    return Math.max(b.h - b.l, Math.abs(b.h - pc), Math.abs(b.l - pc))
  })
}

export function atr(bars: Bar[], period = 14): number[] {
  const tr = trueRange(bars)
  const out: number[] = []
  let prev = 0
  for (let i = 0; i < tr.length; i++) {
    if (i < period) {
      prev = (prev * i + tr[i]!) / (i + 1)
    } else {
      prev = (prev * (period - 1) + tr[i]!) / period
    }
    out.push(prev)
  }
  return out
}

/** ADX Уайлдера (последнее значение). */
export function adx(bars: Bar[], period = 14): number {
  if (bars.length < period * 3) return NaN
  const tr = trueRange(bars)
  let atrS = 0
  let pdmS = 0
  let ndmS = 0
  let adxV = 0
  const dxs: number[] = []
  for (let i = 1; i < bars.length; i++) {
    const up = bars[i]!.h - bars[i - 1]!.h
    const dn = bars[i - 1]!.l - bars[i]!.l
    const pdm = up > dn && up > 0 ? up : 0
    const ndm = dn > up && dn > 0 ? dn : 0
    if (i <= period) {
      atrS += tr[i]!
      pdmS += pdm
      ndmS += ndm
      continue
    }
    atrS = atrS - atrS / period + tr[i]!
    pdmS = pdmS - pdmS / period + pdm
    ndmS = ndmS - ndmS / period + ndm
    const pdi = (100 * pdmS) / atrS
    const ndi = (100 * ndmS) / atrS
    const dx = pdi + ndi === 0 ? 0 : (100 * Math.abs(pdi - ndi)) / (pdi + ndi)
    dxs.push(dx)
    if (dxs.length === period) adxV = dxs.reduce((a, b) => a + b, 0) / period
    else if (dxs.length > period) adxV = (adxV * (period - 1) + dx) / period
  }
  return adxV
}

/** VWAP с дневным сбросом в 00:00 UTC. */
export function vwapDaily(bars: Bar[]): number[] {
  const out: number[] = []
  let day = -1
  let pv = 0
  let vol = 0
  for (const b of bars) {
    const d = Math.floor(b.t / 86400)
    if (d !== day) {
      day = d
      pv = 0
      vol = 0
    }
    const tp = (b.h + b.l + b.c) / 3
    pv += tp * b.v
    vol += b.v
    out.push(vol > 0 ? pv / vol : b.c)
  }
  return out
}

/** Anchored VWAP от бара `anchor`. */
export function avwap(bars: Bar[], anchor: number): (number | null)[] {
  const out: (number | null)[] = []
  let pv = 0
  let vol = 0
  for (let i = 0; i < bars.length; i++) {
    if (i < anchor) {
      out.push(null)
      continue
    }
    const b = bars[i]!
    pv += ((b.h + b.l + b.c) / 3) * b.v
    vol += b.v
    out.push(vol > 0 ? pv / vol : b.c)
  }
  return out
}

/** CVD по свечам: объём со знаком направления бара (оценка, как в ТЗ «CVD считается из свечей»). */
export function cvd(bars: Bar[]): number[] {
  let acc = 0
  return bars.map((b) => {
    const range = b.h - b.l
    const delta = range > 0 ? ((b.c - b.o) / range) * b.v : 0
    acc += delta
    return acc
  })
}

export function volumeProfile(bars: Bar[], binsCount = 40): { poc: number; vah: number; val: number; bins: { price: number; volume: number }[] } {
  let lo = Infinity
  let hi = -Infinity
  for (const b of bars) {
    lo = Math.min(lo, b.l)
    hi = Math.max(hi, b.h)
  }
  const step = (hi - lo) / binsCount || 1
  const vols = new Array<number>(binsCount).fill(0)
  for (const b of bars) {
    const tp = (b.h + b.l + b.c) / 3
    const idx = Math.min(binsCount - 1, Math.max(0, Math.floor((tp - lo) / step)))
    vols[idx] = (vols[idx] ?? 0) + b.v
  }
  let pocIdx = 0
  vols.forEach((v, i) => {
    if (v > (vols[pocIdx] ?? 0)) pocIdx = i
  })
  const total = vols.reduce((a, b) => a + b, 0)
  let loI = pocIdx
  let hiI = pocIdx
  let acc = vols[pocIdx] ?? 0
  while (acc < total * 0.7 && (loI > 0 || hiI < binsCount - 1)) {
    const below = loI > 0 ? (vols[loI - 1] ?? 0) : -1
    const above = hiI < binsCount - 1 ? (vols[hiI + 1] ?? 0) : -1
    if (above >= below) {
      hiI++
      acc += above
    } else {
      loI--
      acc += below
    }
  }
  const center = (i: number) => lo + step * (i + 0.5)
  return {
    poc: roundPrice(center(pocIdx)),
    vah: roundPrice(lo + step * (hiI + 1)),
    val: roundPrice(lo + step * loI),
    bins: vols.map((v, i) => ({ price: roundPrice(center(i)), volume: Math.round(v) })),
  }
}

/** Доверительный интервал Уилсона 95%. */
export function wilson(p: number, n: number, z = 1.96): [number, number] {
  if (n <= 0) return [0, 1]
  const denom = 1 + (z * z) / n
  const center = (p + (z * z) / (2 * n)) / denom
  const half = (z * Math.sqrt((p * (1 - p)) / n + (z * z) / (4 * n * n))) / denom
  return [Math.max(0, center - half), Math.min(1, center + half)]
}

export function series(times: number[], values: (number | null)[], decimals?: number): TimeValue[] {
  const out: TimeValue[] = []
  for (let i = 0; i < times.length; i++) {
    const v = values[i]
    if (v === null || v === undefined || !Number.isFinite(v)) continue
    out.push({ t: times[i]!, v: decimals === undefined ? roundPrice(v) : round(v, decimals) })
  }
  return out
}
