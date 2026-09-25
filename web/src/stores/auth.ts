import { defineStore } from 'pinia'
import { ref } from 'vue'
import { ApiError } from '@/api/client'
import { api } from '@/api/endpoints'

export const useAuthStore = defineStore('auth', () => {
  const username = ref<string | null>(null)
  /** проверка сессии (/api/auth/me) уже выполнялась */
  const checked = ref(false)
  /** ошибка связи при проверке сессии (не 401) */
  const lastError = ref<string | null>(null)
  let pending: Promise<boolean> | null = null

  async function check(force = false): Promise<boolean> {
    if (checked.value && !force) return username.value !== null
    if (pending) return pending
    pending = (async () => {
      try {
        const me = await api.me()
        username.value = me.username
        return true
      } catch (e) {
        // 401 — нет сессии; сетевая ошибка — тоже считаем неавторизованным (экран логина покажет ошибку)
        username.value = null
        lastError.value = e instanceof ApiError && e.status !== 401 ? e.detail : null
        return false
      } finally {
        checked.value = true
        pending = null
      }
    })()
    return pending
  }

  async function login(user: string, password: string): Promise<void> {
    const res = await api.login({ username: user, password })
    username.value = res.username
    checked.value = true
  }

  async function logout(): Promise<void> {
    try {
      await api.logout()
    } finally {
      markLoggedOut()
    }
  }

  /** Сессия потеряна (401 от любого запроса). */
  function markLoggedOut(): void {
    username.value = null
    checked.value = true
  }

  return { username, checked, lastError, check, login, logout, markLoggedOut }
})
