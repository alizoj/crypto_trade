<script setup lang="ts">
import { computed } from 'vue'
import { useScreenerStore } from '@/stores/screener'
import { fmtDuration } from '@/utils/format'

const screener = useScreenerStore()

const view = computed(() => {
  switch (screener.wsState) {
    case 'open':
      return { cls: 'bg-long', text: 'онлайн', tip: 'Live-обновления скринера подключены' }
    case 'connecting':
      return { cls: 'bg-warn', text: 'подключение', tip: 'Устанавливается соединение WebSocket' }
    case 'reconnecting':
      return {
        cls: 'bg-warn animate-pulse',
        text: 'переподключение',
        tip: `Связь потеряна, попытка ${screener.wsAttempt}${screener.wsNextRetryMs ? `, через ${fmtDuration(screener.wsNextRetryMs)}` : ''}`,
      }
    default:
      return { cls: 'bg-short', text: 'нет связи', tip: 'WebSocket отключён' }
  }
})
</script>

<template>
  <span
    class="inline-flex items-center gap-1.5 text-xs text-muted"
    data-testid="ws-status"
    :data-state="screener.wsState"
    :title="view.tip"
    role="status"
  >
    <span class="inline-block h-2.5 w-2.5 rounded-full" :class="view.cls" aria-hidden="true" />
    <span class="hidden sm:inline">{{ view.text }}</span>
  </span>
</template>
