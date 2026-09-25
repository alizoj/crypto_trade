<script setup lang="ts">
// Reliability diagram для live-сигналов: предсказанная vs фактическая частота по корзинам, с ДИ (SVG).
import { computed } from 'vue'
import type { CalibrationLive } from '@/api/types'
import { fmtNum } from '@/utils/format'

const props = defineProps<{ data: CalibrationLive }>()

const W = 340
const H = 300
const M = { l: 44, r: 12, t: 12, b: 38 }

const domain = computed<[number, number]>(() => {
  const b = props.data.buckets
  if (!b.length) return [0, 1]
  let lo = Math.min(...b.map((x) => Math.min(x.p_pred, x.ci_low)))
  let hi = Math.max(...b.map((x) => Math.max(x.p_pred, x.ci_high)))
  lo = Math.max(0, Math.floor((lo - 0.05) * 10) / 10)
  hi = Math.min(1, Math.ceil((hi + 0.05) * 10) / 10)
  return hi - lo < 0.2 ? [Math.max(0, lo - 0.1), Math.min(1, hi + 0.1)] : [lo, hi]
})

const x = (v: number) => M.l + ((v - domain.value[0]) / (domain.value[1] - domain.value[0])) * (W - M.l - M.r)
const y = (v: number) => H - M.b - ((v - domain.value[0]) / (domain.value[1] - domain.value[0])) * (H - M.t - M.b)

const ticks = computed(() => {
  const [lo, hi] = domain.value
  const out: number[] = []
  for (let v = Math.ceil(lo * 10) / 10; v <= hi + 1e-9; v += 0.1) out.push(Math.round(v * 10) / 10)
  return out
})

const maxN = computed(() => Math.max(1, ...props.data.buckets.map((b) => b.n)))
const radius = (n: number) => 3 + 6 * Math.sqrt(n / maxN.value)
</script>

<template>
  <figure class="space-y-2" data-testid="calibration-chart">
    <svg :viewBox="`0 0 ${W} ${H}`" class="block h-auto w-full max-w-[420px]" role="img" aria-label="Калибровка: предсказанная и фактическая частота">
      <title>Калибровка live-сигналов</title>
      <!-- сетка -->
      <g stroke="var(--chart-grid)" stroke-width="1">
        <line v-for="t in ticks" :key="`gx${t}`" :x1="x(t)" :x2="x(t)" :y1="M.t" :y2="H - M.b" />
        <line v-for="t in ticks" :key="`gy${t}`" :x1="M.l" :x2="W - M.r" :y1="y(t)" :y2="y(t)" />
      </g>
      <!-- идеальная калибровка -->
      <line :x1="x(domain[0])" :y1="y(domain[0])" :x2="x(domain[1])" :y2="y(domain[1])" stroke="var(--muted)" stroke-dasharray="4 4" />
      <!-- оси -->
      <g fill="var(--muted)" font-size="10">
        <text v-for="t in ticks" :key="`tx${t}`" :x="x(t)" :y="H - M.b + 14" text-anchor="middle">{{ Math.round(t * 100) }}%</text>
        <text v-for="t in ticks" :key="`ty${t}`" :x="M.l - 6" :y="y(t) + 3" text-anchor="end">{{ Math.round(t * 100) }}%</text>
        <text :x="(M.l + W - M.r) / 2" :y="H - 6" text-anchor="middle" font-size="11">Предсказанная P</text>
        <text :x="12" :y="(M.t + H - M.b) / 2" text-anchor="middle" font-size="11" :transform="`rotate(-90 12 ${(M.t + H - M.b) / 2})`">
          Фактическая частота
        </text>
      </g>
      <!-- корзины -->
      <g v-for="(b, i) in data.buckets" :key="i" :opacity="data.stale ? 0.55 : 1">
        <line :x1="x(b.p_pred)" :x2="x(b.p_pred)" :y1="y(b.ci_low)" :y2="y(b.ci_high)" stroke="var(--accent)" stroke-width="1.5" />
        <line :x1="x(b.p_pred) - 4" :x2="x(b.p_pred) + 4" :y1="y(b.ci_low)" :y2="y(b.ci_low)" stroke="var(--accent)" />
        <line :x1="x(b.p_pred) - 4" :x2="x(b.p_pred) + 4" :y1="y(b.ci_high)" :y2="y(b.ci_high)" stroke="var(--accent)" />
        <circle
          :cx="x(b.p_pred)"
          :cy="y(b.p_obs)"
          :r="radius(b.n)"
          :fill="data.stale ? 'var(--muted)' : 'var(--accent)'"
          fill-opacity="0.75"
          stroke="var(--surface)"
        >
          <title>
            P предсказ. {{ fmtNum(b.p_pred * 100, 1) }}% · факт {{ fmtNum(b.p_obs * 100, 1) }}% (ДИ {{ fmtNum(b.ci_low * 100, 0) }}–{{
              fmtNum(b.ci_high * 100, 0)
            }}%) · n={{ b.n }}
          </title>
        </circle>
      </g>
      <text v-if="!data.buckets.length" :x="W / 2" :y="H / 2" text-anchor="middle" fill="var(--muted)">Нет закрытых live-сигналов</text>
    </svg>
    <figcaption class="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm">
      <span>Brier: <b class="num" data-testid="brier">{{ fmtNum(data.brier, 4) }}</b></span>
      <span>n: <b class="num" data-testid="calib-n">{{ data.n_total }}</b></span>
      <span v-if="data.stale" class="badge badge-warn" data-testid="calib-stale">калибровка устарела</span>
      <span v-else class="badge badge-long">в норме</span>
    </figcaption>
    <details class="text-sm">
      <summary class="cursor-pointer text-muted">Таблица корзин</summary>
      <table class="tbl mt-1 text-[13px]">
        <thead>
          <tr><th>P предсказ.</th><th>Факт</th><th>ДИ 95%</th><th>n</th></tr>
        </thead>
        <tbody>
          <tr v-for="(b, i) in data.buckets" :key="i">
            <td class="num">{{ fmtNum(b.p_pred * 100, 1) }}%</td>
            <td class="num">{{ fmtNum(b.p_obs * 100, 1) }}%</td>
            <td class="num">{{ fmtNum(b.ci_low * 100, 0) }}–{{ fmtNum(b.ci_high * 100, 0) }}%</td>
            <td class="num">{{ b.n }}</td>
          </tr>
        </tbody>
      </table>
    </details>
  </figure>
</template>
