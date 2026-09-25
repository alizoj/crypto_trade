<script setup lang="ts">
import type { SetupStats } from '@/api/types'
import { fmtCi, fmtR, fmtShare } from '@/utils/format'
import { OUTCOME_RU } from '@/utils/labels'
import { signClass } from '@/utils/colors'

defineProps<{ stats: SetupStats | null }>()

function dotClass(o: 'tp' | 'sl' | 'timeout'): string {
  return o === 'tp' ? 'bg-long' : o === 'sl' ? 'bg-short' : 'bg-muted'
}
</script>

<template>
  <div v-if="!stats" class="text-muted-sm">Нет статистики сетапа</div>
  <div v-else class="space-y-1.5 text-[13px]">
    <div class="flex flex-wrap gap-x-4 gap-y-1">
      <span><span class="text-muted">n</span> <b class="num">{{ stats.n }}</b></span>
      <span><span class="text-muted">Winrate</span> <b class="num">{{ fmtShare(stats.winrate, 1) }}</b></span>
      <span><span class="text-muted">ДИ</span> <b class="num">{{ fmtCi(stats.ci_low, stats.ci_high) }}</b></span>
      <span><span class="text-muted">Ср. R</span> <b class="num" :class="signClass(stats.avg_r)">{{ fmtR(stats.avg_r) }}</b></span>
    </div>
    <div class="flex items-center gap-1" aria-label="Последние 10 исходов">
      <span class="mr-1 text-muted-sm">Последние 10:</span>
      <span
        v-for="(o, i) in stats.last10"
        :key="i"
        class="inline-block h-3 w-3 rounded-full"
        :class="dotClass(o)"
        :title="OUTCOME_RU[o]"
        :aria-label="OUTCOME_RU[o]"
      />
    </div>
  </div>
</template>
