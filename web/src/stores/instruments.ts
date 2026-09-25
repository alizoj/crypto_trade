import { defineStore } from 'pinia'
import { computed, ref } from 'vue'
import { api } from '@/api/endpoints'
import type { Instrument } from '@/api/types'

export const useInstrumentsStore = defineStore('instruments', () => {
  const list = ref<Instrument[]>([])
  let pending: Promise<void> | null = null

  function load(force = false): Promise<void> {
    if (list.value.length && !force) return Promise.resolve()
    if (pending) return pending
    pending = api
      .instruments()
      .then((l) => {
        list.value = l.slice().sort((a, b) => a.hh_symbol.localeCompare(b.hh_symbol))
      })
      .catch(() => {
        /* справочник не критичен */
      })
      .finally(() => {
        pending = null
      })
    return pending
  }

  const bySymbol = computed(() => new Map(list.value.map((i) => [i.hh_symbol, i])))

  return { list, bySymbol, load }
})
