<script setup lang="ts">
import { onBeforeUnmount, watch } from 'vue'
import { useRoute } from 'vue-router'
import AppHeader from '@/components/AppHeader.vue'
import { useAuthStore } from '@/stores/auth'
import { useMetaStore } from '@/stores/meta'
import { useAccountsStore } from '@/stores/accounts'
import { useScreenerStore } from '@/stores/screener'
import { usePrefsStore } from '@/stores/prefs'

const route = useRoute()
const auth = useAuthStore()
const meta = useMetaStore()
const accounts = useAccountsStore()
const screener = useScreenerStore()
usePrefsStore() // применяет тему

// Live-данные (WS скринера, мета, счета) — только для авторизованного пользователя.
watch(
  () => auth.username,
  (user, prev) => {
    if (user) {
      void meta.load()
      void accounts.load()
      meta.startClock()
      if (prev && prev !== user) screener.stop()
      screener.start()
    } else {
      screener.stop()
      meta.stopClock()
      accounts.reset()
    }
  },
  { immediate: true },
)

onBeforeUnmount(() => {
  screener.stop()
  meta.stopClock()
})
</script>

<template>
  <RouterView v-if="route.meta.public" />
  <div v-else class="flex min-h-screen flex-col">
    <AppHeader />
    <main class="mx-auto w-full max-w-[1920px] flex-1 px-2 py-3 sm:px-4">
      <RouterView />
    </main>
  </div>
</template>
