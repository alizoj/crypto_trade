<script setup lang="ts">
import { computed } from 'vue'
import type { TimeValue } from '@/api/types'

const props = withDefaults(
  defineProps<{
    points: TimeValue[]
    width?: number
    height?: number
    /** рисовать нулевую линию (для funding) */
    zeroLine?: boolean
    color?: string
    label: string
  }>(),
  { width: 220, height: 48, zeroLine: false, color: 'var(--accent)' },
)

const geo = computed(() => {
  const pts = props.points
  if (pts.length < 2) return null
  let lo = Infinity
  let hi = -Infinity
  for (const p of pts) {
    lo = Math.min(lo, p.v)
    hi = Math.max(hi, p.v)
  }
  if (props.zeroLine) {
    lo = Math.min(lo, 0)
    hi = Math.max(hi, 0)
  }
  const span = hi - lo || Math.abs(hi) || 1
  const pad = 3
  const w = props.width
  const h = props.height
  const t0 = pts[0]!.t
  const t1 = pts[pts.length - 1]!.t
  const x = (t: number) => pad + ((t - t0) / (t1 - t0 || 1)) * (w - pad * 2)
  const y = (v: number) => pad + (1 - (v - lo) / span) * (h - pad * 2)
  const d = pts.map((p, i) => `${i ? 'L' : 'M'}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join(' ')
  const lastPt = pts[pts.length - 1]!
  return { d, zeroY: y(0), lastX: x(lastPt.t), lastY: y(lastPt.v) }
})
</script>

<template>
  <svg
    :viewBox="`0 0 ${width} ${height}`"
    class="block w-full"
    :style="{ height: height + 'px', maxWidth: width + 'px' }"
    role="img"
    :aria-label="label"
    preserveAspectRatio="none"
  >
    <title>{{ label }}</title>
    <template v-if="geo">
      <line v-if="zeroLine" :x1="0" :x2="width" :y1="geo.zeroY" :y2="geo.zeroY" stroke="var(--line)" stroke-dasharray="3 3" />
      <path :d="geo.d" fill="none" :stroke="color" stroke-width="1.5" stroke-linejoin="round" />
      <circle :cx="geo.lastX" :cy="geo.lastY" r="2.5" :fill="color" />
    </template>
    <text v-else x="4" :y="height / 2" fill="var(--muted)" font-size="11">нет данных</text>
  </svg>
</template>
