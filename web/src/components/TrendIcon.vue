<script setup lang="ts">
import { trendArrow, trendClass, trendLabel } from '@/utils/colors'

defineProps<{ value: -1 | 0 | 1 | null | undefined; label?: string }>()
</script>

<template>
  <span
    :class="trendClass(value)"
    :title="`${label ? label + ': ' : ''}${trendLabel(value)}`"
    :aria-label="`${label ? label + ': ' : ''}${trendLabel(value)}`"
    :data-value="value ?? ''"
    >{{ trendArrow(value) }}</span
  >
</template>
