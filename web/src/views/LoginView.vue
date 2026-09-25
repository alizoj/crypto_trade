<script setup lang="ts">
import { computed, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ApiError } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { usePrefsStore } from '@/stores/prefs'

const auth = useAuthStore()
usePrefsStore()
const router = useRouter()
const route = useRoute()
const isMock = import.meta.env.VITE_MOCK === '1'

const username = ref('')
const password = ref('')
const error = ref<string | null>(auth.lastError)
const busy = ref(false)

const canSubmit = computed(() => username.value.trim() !== '' && password.value !== '' && !busy.value)

async function submit(): Promise<void> {
  if (!canSubmit.value) return
  busy.value = true
  error.value = null
  try {
    await auth.login(username.value.trim(), password.value)
    const redirect = typeof route.query.redirect === 'string' && route.query.redirect.startsWith('/') ? route.query.redirect : '/'
    await router.replace(redirect)
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) {
      error.value = e.detail && e.detail !== 'unauthorized' ? e.detail : 'Неверное имя пользователя или пароль'
    } else if (e instanceof ApiError && e.status === 429) {
      error.value = 'Слишком много попыток. Подождите и попробуйте снова'
    } else if (e instanceof ApiError) {
      error.value = e.status === 0 ? e.detail : `Ошибка сервера (${e.status})`
    } else {
      error.value = String(e)
    }
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="flex min-h-screen items-center justify-center px-4 py-10">
    <form class="card w-full max-w-sm space-y-4 p-6" data-testid="login-form" @submit.prevent="submit">
      <div class="flex items-center gap-2">
        <img src="/favicon.svg" alt="" class="h-8 w-8" />
        <div>
          <h1 class="text-lg font-semibold">HashHedge Quant</h1>
          <p class="text-muted-sm">Вход по паролю</p>
        </div>
      </div>

      <div>
        <label class="label" for="login-user">Имя пользователя</label>
        <input id="login-user" v-model="username" class="input w-full" name="username" autocomplete="username" required autofocus />
      </div>
      <div>
        <label class="label" for="login-pass">Пароль</label>
        <input
          id="login-pass"
          v-model="password"
          class="input w-full"
          name="password"
          type="password"
          autocomplete="current-password"
          required
        />
      </div>
      <p v-if="error" class="rounded-md border border-short/40 bg-short/10 px-3 py-2 text-sm text-short" role="alert" data-testid="login-error">
        {{ error }}
      </p>

      <button type="submit" class="btn btn-primary w-full" :disabled="!canSubmit">
        {{ busy ? 'Проверка…' : 'Войти' }}
      </button>

      <p v-if="isMock" class="text-muted-sm">Mock-режим: любое имя и пароль.</p>
    </form>
  </div>
</template>
