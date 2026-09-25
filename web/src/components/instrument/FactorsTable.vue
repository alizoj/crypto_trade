<script setup lang="ts">
import { computed } from 'vue'
import type { Factor, FactorVerdict } from '@/api/types'

const props = defineProps<{ long: Factor[]; short: Factor[] }>()

interface Row {
  name: string
  label: string
  value: Factor['value']
  long: Factor | null
  short: Factor | null
}

const rows = computed<Row[]>(() => {
  const byName = new Map<string, Row>()
  for (const f of props.long) byName.set(f.name, { name: f.name, label: f.label, value: f.value, long: f, short: null })
  for (const f of props.short) {
    const r = byName.get(f.name)
    if (r) r.short = f
    else byName.set(f.name, { name: f.name, label: f.label, value: f.value, long: null, short: f })
  }
  return [...byName.values()]
})

const totals = computed(() => ({
  long: props.long.reduce((a, f) => a + f.points, 0),
  longMax: props.long.reduce((a, f) => a + f.max_points, 0),
  short: props.short.reduce((a, f) => a + f.points, 0),
  shortMax: props.short.reduce((a, f) => a + f.max_points, 0),
}))

const VERDICT: Record<FactorVerdict, { text: string; cls: string }> = {
  pro: { text: 'за', cls: 'badge-long' },
  con: { text: 'против', cls: 'badge-short' },
  neutral: { text: 'нейтр.', cls: 'badge-gray' },
}

function fmtValue(v: Factor['value']): string {
  if (v === null) return '—'
  if (typeof v === 'number') return Number.isInteger(v) ? String(v) : v.toFixed(Math.abs(v) < 10 ? 2 : 1)
  return v
}
</script>

<template>
  <div class="overflow-x-auto">
    <table class="tbl text-[13px]" data-testid="factors-table">
      <thead>
        <tr>
          <th class="left" scope="col">Фактор</th>
          <th scope="col">Значение</th>
          <th scope="col">Лонг</th>
          <th scope="col">Шорт</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.name">
          <td class="left whitespace-normal">{{ r.label }}</td>
          <td class="num">{{ fmtValue(r.value) }}</td>
          <td>
            <template v-if="r.long">
              <span v-if="r.long.max_points > 0" class="num mr-1">{{ r.long.points }}/{{ r.long.max_points }}</span>
              <span class="badge" :class="VERDICT[r.long.verdict].cls">{{ VERDICT[r.long.verdict].text }}</span>
            </template>
          </td>
          <td>
            <template v-if="r.short">
              <span v-if="r.short.max_points > 0" class="num mr-1">{{ r.short.points }}/{{ r.short.max_points }}</span>
              <span class="badge" :class="VERDICT[r.short.verdict].cls">{{ VERDICT[r.short.verdict].text }}</span>
            </template>
          </td>
        </tr>
      </tbody>
      <tfoot>
        <tr>
          <td class="left font-semibold">Итого score</td>
          <td />
          <td class="num font-semibold">{{ totals.long }}/{{ totals.longMax }}</td>
          <td class="num font-semibold">{{ totals.short }}/{{ totals.shortMax }}</td>
        </tr>
      </tfoot>
    </table>
  </div>
</template>
