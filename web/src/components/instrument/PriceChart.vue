<script setup lang="ts">
// График инструмента на Lightweight Charts v5: три панели на одной шкале времени
// (свечи + EMA/VWAP/AVWAP/уровни VP/маркеры/линии сигнала; RSI; объём + CVD) — скролл синхронизирован.
import { onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import {
  CandlestickSeries,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  LineStyle,
  createChart,
  createSeriesMarkers,
  type IChartApi,
  type ISeriesApi,
  type LogicalRange,
  type SeriesMarker,
  type Time,
  type UTCTimestamp,
} from 'lightweight-charts'
import type { BarsResponse, Signal, TimeValue } from '@/api/types'
import type { TzMode } from '@/utils/format'
import { fmtPrice, priceDecimals } from '@/utils/format'
import { crosshairLabel, tickLabel } from '@/utils/chartTime'

export interface Overlays {
  ema: boolean
  vwap: boolean
  avwap: boolean
  levels: boolean
  markers: boolean
}

const props = defineProps<{
  data: BarsResponse
  tz: TzMode
  /** меняется при смене темы — перерисовка с новыми цветами */
  themeKey: string
  overlays: Overlays
  /** сигнал, линии которого показывать (по умолчанию data.active_signal) */
  signal?: Signal | null
}>()

const el = ref<HTMLDivElement | null>(null)
const chart = shallowRef<IChartApi | null>(null)
let candles: ISeriesApi<'Candlestick'> | null = null
const legend = ref<{ t: number; o: number; h: number; l: number; c: number; v: number | null } | null>(null)
let lastKey = ''

function cssVar(name: string, fallback: string): string {
  try {
    const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
    return v || fallback
  } catch {
    return fallback
  }
}

const ts = (t: number) => t as UTCTimestamp
const line = (arr: TimeValue[]) => arr.map((p) => ({ time: ts(p.t), value: p.v }))

function destroy(): void {
  chart.value?.remove()
  chart.value = null
  candles = null
}

function build(): void {
  if (!el.value) return
  const key = `${props.data.tf}|${props.data.bars.length ? props.data.bars[0]!.t : 0}`
  const prevRange: LogicalRange | null = key === lastKey ? (chart.value?.timeScale().getVisibleLogicalRange() ?? null) : null
  lastKey = key
  destroy()

  const c = {
    bg: cssVar('--surface', '#fff'),
    text: cssVar('--chart-text', '#334155'),
    grid: cssVar('--chart-grid', '#e8ecf2'),
    line: cssVar('--line', '#d9dee7'),
    long: cssVar('--long', '#15803d'),
    short: cssVar('--short', '#c62828'),
    accent: cssVar('--accent', '#2563eb'),
    muted: cssVar('--muted', '#5b6678'),
    ema20: cssVar('--ema20', '#f59e0b'),
    ema50: cssVar('--ema50', '#3b82f6'),
    ema200: cssVar('--ema200', '#8b5cf6'),
    vwap: cssVar('--vwap', '#0891b2'),
    avwap: cssVar('--avwap', '#db2777'),
    level: cssVar('--level', '#64748b'),
    longRgb: cssVar('--long-rgb', '22 163 74'),
    shortRgb: cssVar('--short-rgb', '220 38 38'),
  }
  const tz = props.tz
  const lastPrice = props.data.bars.at(-1)?.c ?? 1
  const decimals = priceDecimals(lastPrice)

  const ch = createChart(el.value, {
    autoSize: true,
    layout: {
      background: { color: c.bg },
      textColor: c.text,
      fontSize: 11,
      panes: { separatorColor: c.line, separatorHoverColor: c.grid, enableResize: true },
      attributionLogo: true,
    },
    grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
    rightPriceScale: { borderColor: c.line },
    timeScale: {
      borderColor: c.line,
      timeVisible: true,
      secondsVisible: false,
      rightOffset: 6,
      tickMarkFormatter: (time: Time, kind: number) => tickLabel(time as number, kind, tz),
    },
    localization: {
      locale: 'ru-RU',
      timeFormatter: (time: Time) => crosshairLabel(time as number, tz),
    },
    crosshair: { mode: CrosshairMode.Normal },
  })
  chart.value = ch

  // --- панель 0: свечи и оверлеи ---
  candles = ch.addSeries(CandlestickSeries, {
    upColor: c.long,
    downColor: c.short,
    borderUpColor: c.long,
    borderDownColor: c.short,
    wickUpColor: c.long,
    wickDownColor: c.short,
    priceFormat: { type: 'price', precision: decimals, minMove: Math.pow(10, -decimals) },
  })
  candles.setData(props.data.bars.map((b) => ({ time: ts(b.t), open: b.o, high: b.h, low: b.l, close: b.c })))

  const ind = props.data.indicators
  const overlay = (data: TimeValue[], color: string, width: 1 | 2 = 1, style: LineStyle = LineStyle.Solid) => {
    const s = ch.addSeries(LineSeries, {
      color,
      lineWidth: width,
      lineStyle: style,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
      priceFormat: { type: 'price', precision: decimals, minMove: Math.pow(10, -decimals) },
    })
    s.setData(line(data))
    return s
  }
  if (props.overlays.ema) {
    overlay(ind.ema20, c.ema20)
    overlay(ind.ema50, c.ema50)
    overlay(ind.ema200, c.ema200, 2)
  }
  if (props.overlays.vwap) overlay(ind.vwap, c.vwap, 1, LineStyle.Dashed)
  if (props.overlays.avwap) overlay(ind.avwap, c.avwap, 1, LineStyle.Dashed)

  const vp = props.data.volume_profile
  if (props.overlays.levels && vp) {
    candles.createPriceLine({ price: vp.poc, color: c.level, lineWidth: 2, lineStyle: LineStyle.Solid, axisLabelVisible: true, title: 'POC' })
    candles.createPriceLine({ price: vp.vah, color: c.level, lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: 'VAH' })
    candles.createPriceLine({ price: vp.val, color: c.level, lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: true, title: 'VAL' })
  }

  const sig = props.signal === undefined ? props.data.active_signal : props.signal
  if (sig) {
    candles.createPriceLine({ price: sig.entry, color: c.accent, lineWidth: 2, lineStyle: LineStyle.Solid, axisLabelVisible: true, title: 'Вход' })
    candles.createPriceLine({ price: sig.sl, color: c.short, lineWidth: 2, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'SL' })
    candles.createPriceLine({ price: sig.tp1, color: c.long, lineWidth: 2, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'TP1' })
    if (sig.tp2 !== null) candles.createPriceLine({ price: sig.tp2, color: c.long, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: 'TP2' })
  }

  if (props.overlays.markers && props.data.markers.length) {
    const times = new Set(props.data.bars.map((b) => b.t))
    const markers: SeriesMarker<Time>[] = props.data.markers
      .filter((m) => times.has(m.t))
      .sort((a, b) => a.t - b.t)
      .map((m) => {
        const long = m.side === 'long'
        const color = m.kind === 'exit' ? c.muted : long ? c.long : c.short
        return {
          time: ts(m.t),
          position: m.kind === 'exit' ? (long ? 'aboveBar' : 'belowBar') : long ? 'belowBar' : 'aboveBar',
          shape: m.kind === 'signal' ? (long ? 'arrowUp' : 'arrowDown') : m.kind === 'entry' ? 'circle' : 'square',
          color,
          text: m.text,
          size: m.kind === 'signal' ? 1.2 : 0.8,
        }
      })
    createSeriesMarkers(candles, markers)
  }

  // --- панель 1: RSI ---
  const rsi = ch.addSeries(
    LineSeries,
    { color: c.accent, lineWidth: 1, priceLineVisible: false, priceFormat: { type: 'price', precision: 1, minMove: 0.1 } },
    1,
  )
  rsi.setData(line(ind.rsi))
  rsi.createPriceLine({ price: 70, color: c.short, lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: '' })
  rsi.createPriceLine({ price: 30, color: c.long, lineWidth: 1, lineStyle: LineStyle.Dotted, axisLabelVisible: false, title: '' })

  // --- панель 2: объём + CVD ---
  const vol = ch.addSeries(
    HistogramSeries,
    { priceFormat: { type: 'volume' }, priceLineVisible: false, lastValueVisible: false },
    2,
  )
  vol.setData(
    ind.volume.map((p) => ({ time: ts(p.t), value: p.v, color: `rgb(${p.up ? c.longRgb : c.shortRgb} / 0.5)` })),
  )
  const cvdS = ch.addSeries(
    LineSeries,
    { color: c.vwap, lineWidth: 1, priceScaleId: 'cvd', lastValueVisible: false, priceLineVisible: false, priceFormat: { type: 'volume' } },
    2,
  )
  cvdS.setData(line(ind.cvd))
  ch.priceScale('cvd', 2).applyOptions({ scaleMargins: { top: 0.1, bottom: 0.1 } })

  const panes = ch.panes()
  panes[0]?.setStretchFactor(3.2)
  panes[1]?.setStretchFactor(1)
  panes[2]?.setStretchFactor(1.1)

  ch.subscribeCrosshairMove((param) => {
    if (!candles || param.time === undefined) {
      legend.value = null
      return
    }
    const d = param.seriesData.get(candles) as { open: number; high: number; low: number; close: number } | undefined
    const v = param.seriesData.get(vol) as { value: number } | undefined
    legend.value = d ? { t: param.time as number, o: d.open, h: d.high, l: d.low, c: d.close, v: v?.value ?? null } : null
  })

  if (prevRange) ch.timeScale().setVisibleLogicalRange(prevRange)
  else {
    const n = props.data.bars.length
    ch.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 160), to: n + 4 })
  }
}

/** Live-обновление последней свечи ценой из WS скринера. */
function updateLastPrice(price: number): void {
  const b = props.data.bars.at(-1)
  if (!candles || !b) return
  candles.update({ time: ts(b.t), open: b.o, high: Math.max(b.h, price), low: Math.min(b.l, price), close: price })
}

function resetView(): void {
  const n = props.data.bars.length
  chart.value?.timeScale().setVisibleLogicalRange({ from: Math.max(0, n - 160), to: n + 4 })
}

defineExpose({ updateLastPrice, resetView })

onMounted(build)
onBeforeUnmount(destroy)
watch(
  () => [props.data, props.tz, props.themeKey, props.overlays, props.signal],
  () => build(),
  { deep: true },
)
</script>

<template>
  <div class="relative h-[460px] w-full sm:h-[600px]" data-testid="price-chart">
    <div ref="el" class="absolute inset-0" />
    <div
      v-if="legend"
      class="num pointer-events-none absolute top-1 left-2 z-10 rounded bg-surface/80 px-1.5 py-0.5 text-[11px] text-muted"
    >
      <span class="text-fg">{{ crosshairLabel(legend.t, tz) }}</span>
      O <b class="text-fg">{{ fmtPrice(legend.o) }}</b> H <b class="text-fg">{{ fmtPrice(legend.h) }}</b> L
      <b class="text-fg">{{ fmtPrice(legend.l) }}</b> C
      <b :class="legend.c >= legend.o ? 'text-long' : 'text-short'">{{ fmtPrice(legend.c) }}</b>
    </div>
  </div>
</template>
