<script setup lang="ts">
import { computed } from 'vue'
import type { ProbInfo, ScreenerStatus, Side } from '@/api/types'
import { probBackground, probView } from '@/utils/colors'

const props = defineProps<{
  prob: ProbInfo | null
  side: Side
  status?: ScreenerStatus
  stale?: boolean
  score?: number | null
  /** показывать ДИ/n второй строкой (карточки, панель анализа) */
  detailed?: boolean
}>()

const v = computed(() => probView(props.prob, props.side, { status: props.status, stale: props.stale, score: props.score }))
const bg = computed(() => probBackground(v.value))
</script>

<template>
  <span
    class="tip inline-flex min-w-[3.6rem] flex-col items-end rounded-md px-1.5 py-0.5 leading-tight"
    :class="[
      v.kind === 'long' ? 'text-long' : v.kind === 'short' ? 'text-short' : 'text-muted',
      v.kind !== 'gray' ? 'font-semibold' : '',
    ]"
    :style="{ background: bg }"
    :title="v.tooltip"
    :data-tip="v.tooltip"
    :data-kind="v.kind"
    :data-gray="v.grayReason ?? ''"
    tabindex="0"
  >
    <span class="num" data-part="label">{{ v.label }}</span>
    <span v-if="v.grayReason === 'uncalibrated'" class="text-[10.5px] font-normal">не откалибр.</span>
    <span v-else-if="detailed && v.sublabel" class="num text-[10.5px] font-normal opacity-80">
      {{ v.sublabel }}<template v-if="prob && v.grayReason !== 'low_data'"> · n={{ prob.n }}</template>
    </span>
  </span>
</template>
