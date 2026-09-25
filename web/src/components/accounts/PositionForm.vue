<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import type { PositionCreate, Side } from '@/api/types'
import { useInstrumentsStore } from '@/stores/instruments'
import { useScreenerStore } from '@/stores/screener'
import { fmtUsd } from '@/utils/format'

const emit = defineEmits<{ submit: [body: PositionCreate]; cancel: [] }>()
defineProps<{ busy?: boolean }>()

const instruments = useInstrumentsStore()
const screener = useScreenerStore()
void instruments.load()

const form = reactive({ hh_symbol: '', side: 'long' as Side, qty: '', entry_price: '', sl: '', tp: '', signal_id: '' })
const error = ref<string | null>(null)

const num = (s: string) => {
  const v = Number(s.replace(',', '.').trim())
  return s.trim() !== '' && Number.isFinite(v) ? v : null
}
const notional = computed(() => {
  const q = num(form.qty)
  const p = num(form.entry_price)
  return q !== null && p !== null ? q * p : null
})
const limit = computed(() => instruments.bySymbol.get(form.hh_symbol)?.max_per_lot_usd ?? null)
const overLimit = computed(() => notional.value !== null && limit.value !== null && notional.value > limit.value)

function onSymbol(): void {
  const row = screener.findRow(form.hh_symbol)
  if (row?.price && !form.entry_price) form.entry_price = String(row.price)
}

function submit(): void {
  error.value = null
  const qty = num(form.qty)
  const entry = num(form.entry_price)
  if (!form.hh_symbol) return void (error.value = 'Выберите инструмент')
  if (qty === null || qty <= 0) return void (error.value = 'Количество должно быть > 0')
  if (entry === null || entry <= 0) return void (error.value = 'Цена входа должна быть > 0')
  const body: PositionCreate = { hh_symbol: form.hh_symbol, side: form.side, qty, entry_price: entry }
  const sl = num(form.sl)
  const tp = num(form.tp)
  const sig = num(form.signal_id)
  if (sl !== null) body.sl = sl
  if (tp !== null) body.tp = tp
  if (sig !== null) body.signal_id = Math.trunc(sig)
  emit('submit', body)
}
</script>

<template>
  <form class="grid grid-cols-2 gap-2 rounded-md bg-surface-2 p-3 sm:grid-cols-4 lg:grid-cols-8" data-testid="position-form" @submit.prevent="submit">
    <div class="col-span-2">
      <label class="label">Инструмент</label>
      <select v-model="form.hh_symbol" class="input w-full" required data-testid="pos-symbol" @change="onSymbol">
        <option value="" disabled>— выберите —</option>
        <option v-for="i in instruments.list" :key="i.hh_symbol" :value="i.hh_symbol">{{ i.hh_symbol }}</option>
      </select>
    </div>
    <div>
      <label class="label">Сторона</label>
      <select v-model="form.side" class="input w-full" data-testid="pos-side">
        <option value="long">Лонг</option>
        <option value="short">Шорт</option>
      </select>
    </div>
    <div>
      <label class="label">Кол-во</label>
      <input v-model="form.qty" class="input num w-full" inputmode="decimal" required data-testid="pos-qty" />
    </div>
    <div>
      <label class="label">Цена входа</label>
      <input v-model="form.entry_price" class="input num w-full" inputmode="decimal" required data-testid="pos-entry" />
    </div>
    <div>
      <label class="label">SL</label>
      <input v-model="form.sl" class="input num w-full" inputmode="decimal" />
    </div>
    <div>
      <label class="label">TP</label>
      <input v-model="form.tp" class="input num w-full" inputmode="decimal" />
    </div>
    <div>
      <label class="label">ID сигнала</label>
      <input v-model="form.signal_id" class="input num w-full" inputmode="numeric" />
    </div>
    <div class="col-span-2 flex flex-wrap items-center gap-2 sm:col-span-4 lg:col-span-8">
      <span class="text-muted-sm num">
        Notional: <b :class="overLimit ? 'text-short' : 'text-fg'">{{ fmtUsd(notional, 2) }}</b>
        <template v-if="limit !== null"> · лимит max_per_lot {{ fmtUsd(limit) }}</template>
      </span>
      <span v-if="overLimit" class="text-sm text-short">Превышен лимит HashHedge на позицию</span>
      <span v-if="error" class="text-sm text-short" role="alert">{{ error }}</span>
      <div class="ml-auto flex gap-2">
        <button type="button" class="btn btn-sm" @click="emit('cancel')">Отмена</button>
        <button type="submit" class="btn btn-sm btn-primary" :disabled="busy" data-testid="pos-submit">Добавить позицию</button>
      </div>
    </div>
  </form>
</template>
