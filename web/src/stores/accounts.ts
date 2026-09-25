// Счета (челленджи): список, выбранный счёт для шапки и плана сделки, CRUD.
import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '@/api/endpoints'
import type { Account, AccountCreate, AccountPatch, PositionCreate } from '@/api/types'
import { errorMessage } from '@/api/client'
import { usePrefsStore } from './prefs'

/** Вставка/замена счёта по id без мутации исходного массива. */
export function upsertAccount(list: Account[], acc: Account): Account[] {
  const idx = list.findIndex((a) => a.id === acc.id)
  if (idx === -1) return [...list, acc]
  const next = list.slice()
  next[idx] = acc
  return next
}

export const useAccountsStore = defineStore('accounts', () => {
  const prefs = usePrefsStore()
  const list = ref<Account[]>([])
  const loaded = ref(false)
  const loading = ref(false)
  const error = ref<string | null>(null)

  const selected = computed<Account | null>(() => {
    if (!list.value.length) return null
    return list.value.find((a) => a.id === prefs.accountId) ?? list.value.find((a) => a.status === 'active') ?? list.value[0] ?? null
  })

  async function load(): Promise<void> {
    loading.value = true
    try {
      list.value = await api.accounts()
      loaded.value = true
      error.value = null
    } catch (e) {
      error.value = errorMessage(e)
    } finally {
      loading.value = false
    }
  }

  function select(id: number | null): void {
    prefs.accountId = id
  }

  /** Обновление из WS-сообщения {type:"account"}. */
  function applyAccount(acc: Account): void {
    list.value = upsertAccount(list.value, acc)
  }

  async function create(body: AccountCreate): Promise<Account> {
    const acc = await api.createAccount(body)
    applyAccount(acc)
    return acc
  }

  async function patch(id: number, body: AccountPatch): Promise<Account> {
    const acc = await api.patchAccount(id, body)
    applyAccount(acc)
    return acc
  }

  async function remove(id: number): Promise<void> {
    await api.deleteAccount(id)
    list.value = list.value.filter((a) => a.id !== id)
    if (prefs.accountId === id) prefs.accountId = null
  }

  async function addPosition(id: number, body: PositionCreate): Promise<Account> {
    const acc = await api.addPosition(id, body)
    applyAccount(acc)
    return acc
  }

  async function closePosition(id: number, posId: number, exitPrice?: number): Promise<Account> {
    const acc = await api.closePosition(id, posId, exitPrice)
    applyAccount(acc)
    return acc
  }

  function reset(): void {
    list.value = []
    loaded.value = false
  }

  return { list, loaded, loading, error, selected, load, select, applyAccount, create, patch, remove, addPosition, closePosition, reset }
})
