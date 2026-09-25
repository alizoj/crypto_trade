<script setup lang="ts">
import { computed } from 'vue'

/** Полоса «запас до лимита»: room — сколько осталось, limit — размер лимита в $. */
const props = defineProps<{ room: number | null; limit: number | null; compact?: boolean }>()

const share = computed(() => {
  if (props.room === null || props.limit === null || props.limit <= 0) return null
  return Math.min(1, Math.max(0, props.room / props.limit))
})

const color = computed(() => {
  const s = share.value
  if (s === null) return 'var(--muted)'
  if (s < 0.25) return 'var(--short)'
  if (s < 0.5) return 'var(--warn)'
  return 'var(--long)'
})
</script>

<template>
  <span
    class="relative inline-block overflow-hidden rounded-full bg-surface-2"
    :class="compact ? 'h-1.5 w-14' : 'h-2 w-full'"
    role="meter"
    :aria-valuenow="share === null ? undefined : Math.round(share * 100)"
    aria-valuemin="0"
    aria-valuemax="100"
  >
    <span
      class="absolute inset-y-0 left-0 rounded-full"
      :style="{ width: `${(share ?? 0) * 100}%`, background: color }"
    />
  </span>
</template>
