<script setup lang="ts">
import { computed } from 'vue'
import type { ScreenerStatus, Side } from '@/api/types'
import { SCREENER_STATUS_RU } from '@/utils/labels'

const props = defineProps<{ status: ScreenerStatus; side?: Side | null; ageSec?: number | null }>()

const cls = computed(() => {
  switch (props.status) {
    case 'signal':
      return props.side === 'short' ? 'badge-short' : props.side === 'long' ? 'badge-long' : 'badge-accent'
    case 'watch':
      return 'badge-accent'
    case 'halted':
      return 'badge-warn'
    default:
      return 'badge-gray'
  }
})

const text = computed(() => {
  if (props.status === 'signal' && props.side) return `${SCREENER_STATUS_RU.signal} ${props.side === 'long' ? '▲' : '▼'}`
  return SCREENER_STATUS_RU[props.status]
})

const tip = computed(() => {
  const base: Record<ScreenerStatus, string> = {
    signal: 'Активный сигнал',
    watch: 'Наблюдение: сетап формируется',
    none: 'Нет сетапа',
    off_session: 'Вне сессии базового рынка — сигналы по TradFi выключены',
    halted: 'Торги приостановлены / замороженные бары',
  }
  const side = props.side ? ` (${props.side === 'long' ? 'лонг' : 'шорт'})` : ''
  return `${base[props.status]}${props.status === 'signal' ? side : ''}`
})
</script>

<template>
  <span class="badge" :class="cls" :title="tip" :data-status="status">{{ text }}</span>
</template>
