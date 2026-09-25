<script setup lang="ts">
import { computed } from 'vue'
import { useAccountsStore } from '@/stores/accounts'
import { useMetaStore } from '@/stores/meta'
import { usePrefsStore } from '@/stores/prefs'
import { fmtDateTime, fmtDuration, fmtUsd } from '@/utils/format'
import { STAGE_RU } from '@/utils/labels'
import RoomBar from './RoomBar.vue'

const accounts = useAccountsStore()
const meta = useMetaStore()
const prefs = usePrefsStore()

const acc = computed(() => accounts.selected)

const resetText = computed(() => {
  const r = meta.reset
  if (r.inWindow) return `окно сброса: ещё ${fmtDuration(r.msToEnd)}`
  return `до ресета ${fmtDuration(r.msToStart)}`
})
const resetTip = computed(
  () =>
    `Окно дневного сброса ${meta.window.start_utc}–${meta.window.end_utc} UTC. Ближайшее: ${fmtDateTime(meta.reset.nextStartMs, prefs.tz)}. Входы и выходы внутри окна не планируются.`,
)

function onSelect(e: Event): void {
  const v = Number((e.target as HTMLSelectElement).value)
  accounts.select(Number.isFinite(v) ? v : null)
}
</script>

<template>
  <div class="flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-xs" data-testid="account-room">
    <template v-if="acc">
      <label class="sr-only" for="hdr-account">Счёт</label>
      <select
        id="hdr-account"
        class="input max-w-[11rem] truncate py-0.5 text-xs"
        :value="acc.id"
        title="Счёт для шапки и расчёта плана сделки"
        @change="onSelect"
      >
        <option v-for="a in accounts.list" :key="a.id" :value="a.id">{{ a.name }} · {{ STAGE_RU[a.stage] }}</option>
      </select>
      <span class="flex items-center gap-1.5" :title="`Запас до дневного лимита ${fmtUsd(acc.daily_limit_usd)} (floor ${fmtUsd(acc.daily_floor, 2)})`">
        <span class="text-muted">День</span>
        <b class="num" data-testid="hdr-daily-room" :class="acc.daily_room_usd <= 0 ? 'text-short' : ''">{{ fmtUsd(acc.daily_room_usd) }}</b>
        <RoomBar :room="acc.daily_room_usd" :limit="acc.daily_limit_usd" compact />
      </span>
      <span class="flex items-center gap-1.5" :title="`Запас до общего лимита ${fmtUsd(acc.total_limit_usd)} (floor ${fmtUsd(acc.total_floor, 2)})`">
        <span class="text-muted">Всего</span>
        <b class="num" data-testid="hdr-total-room">{{ fmtUsd(acc.total_room_usd) }}</b>
        <RoomBar :room="acc.total_room_usd" :limit="acc.total_limit_usd" compact />
      </span>
      <span v-if="acc.soft_stop_hit" class="badge badge-short">soft stop</span>
      <span v-if="acc.target_lock" class="badge badge-warn" title="Цель достигнута — добор торговых дней минимальным риском">target lock</span>
    </template>
    <span v-else-if="accounts.loaded" class="text-muted">
      Нет счетов — <RouterLink to="/accounts" class="text-accent underline">создать</RouterLink>
    </span>
    <span
      class="num flex items-center gap-1"
      :class="meta.reset.inWindow ? 'font-semibold text-warn' : 'text-muted'"
      :title="resetTip"
      data-testid="reset-countdown"
    >
      <span aria-hidden="true">⏱</span>{{ resetText }}
    </span>
  </div>
</template>
