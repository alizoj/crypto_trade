<script setup lang="ts">
import { computed } from 'vue'
import { useRouter } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import { usePrefsStore } from '@/stores/prefs'
import { tzLabel } from '@/utils/format'
import AccountRoom from './AccountRoom.vue'
import WsIndicator from './WsIndicator.vue'

const auth = useAuthStore()
const prefs = usePrefsStore()
const router = useRouter()
const isMock = import.meta.env.VITE_MOCK === '1'

const nav = [
  { to: '/', label: 'Скринер', exact: true },
  { to: '/accounts', label: 'Счета' },
  { to: '/signals', label: 'Журнал' },
  { to: '/backtests', label: 'Бэктесты' },
]

const themeLabel = computed(() => ({ system: 'Системная', light: 'Светлая', dark: 'Тёмная' })[prefs.theme])
const themeIcon = computed(() => ({ system: '◐', light: '☀', dark: '☾' })[prefs.theme])
const tzText = computed(() => (prefs.tz === 'utc' ? 'UTC' : tzLabel('local')))

async function logout(): Promise<void> {
  await auth.logout().catch(() => undefined)
  await router.replace('/login')
}
</script>

<template>
  <header class="sticky top-0 z-40 border-b border-line bg-surface/95 backdrop-blur">
    <div class="mx-auto flex max-w-[1920px] flex-wrap items-center gap-x-4 gap-y-2 px-2 py-2 sm:px-4">
      <RouterLink to="/" class="flex items-center gap-2 font-semibold whitespace-nowrap">
        <img src="/favicon.svg" alt="" class="h-6 w-6" />
        <span>HashHedge <span class="text-muted">Quant</span></span>
        <span v-if="isMock" class="badge badge-warn" title="Данные эмулируются в браузере (VITE_MOCK=1)">mock</span>
      </RouterLink>

      <nav class="order-3 flex w-full gap-1 md:order-none md:w-auto" aria-label="Разделы">
        <RouterLink
          v-for="item in nav"
          :key="item.to"
          :to="item.to"
          class="rounded-md px-2.5 py-1 text-sm font-medium text-muted hover:bg-surface-2 hover:text-fg"
          :active-class="item.exact ? '' : 'bg-surface-2 !text-fg'"
          :exact-active-class="'bg-surface-2 !text-fg'"
        >
          {{ item.label }}
        </RouterLink>
      </nav>

      <div class="order-4 w-full md:order-none md:w-auto md:flex-1">
        <AccountRoom />
      </div>

      <div class="ml-auto flex items-center gap-2">
        <WsIndicator />
        <button
          type="button"
          class="btn btn-sm num"
          data-testid="tz-toggle"
          :title="`Время: ${prefs.tz === 'utc' ? 'UTC' : 'локальное'}. Нажмите, чтобы переключить`"
          @click="prefs.toggleTz()"
        >
          {{ tzText }}
        </button>
        <button
          type="button"
          class="btn btn-sm"
          data-testid="theme-toggle"
          :title="`Тема: ${themeLabel}. Нажмите, чтобы переключить`"
          :aria-label="`Тема: ${themeLabel}`"
          @click="prefs.cycleTheme()"
        >
          <span aria-hidden="true">{{ themeIcon }}</span>
        </button>
        <button type="button" class="btn btn-sm" :title="`Выйти (${auth.username ?? ''})`" @click="logout">Выйти</button>
      </div>
    </div>
  </header>
</template>
