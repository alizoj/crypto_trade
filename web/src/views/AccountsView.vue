<script setup lang="ts">
import { computed, reactive, ref, watch } from 'vue'
import type { ChallengeType, Stage } from '@/api/types'
import { errorMessage } from '@/api/client'
import { useAccountsStore } from '@/stores/accounts'
import AccountCard from '@/components/accounts/AccountCard.vue'
import { ACCOUNT_SIZES, CHALLENGE_RU, STAGE_RU } from '@/utils/labels'
import { fmtUsd } from '@/utils/format'

const accounts = useAccountsStore()
if (!accounts.loaded) void accounts.load()

const showCreate = ref(false)
const busy = ref(false)
const error = ref<string | null>(null)
const form = reactive<{ name: string; challenge_type: ChallengeType; size: number; stage: Stage }>({
  name: '',
  challenge_type: 'two_stage',
  size: 10000,
  stage: 'stage1',
})
const sizes = computed(() => ACCOUNT_SIZES[form.challenge_type])
const stages = computed<Stage[]>(() => (form.challenge_type === 'one_stage' ? ['stage1', 'trader'] : ['stage1', 'stage2', 'trader']))
watch(
  () => form.challenge_type,
  () => {
    if (!sizes.value.includes(form.size)) form.size = sizes.value[0]!
    if (!stages.value.includes(form.stage)) form.stage = 'stage1'
  },
)

async function create(): Promise<void> {
  if (!form.name.trim()) {
    error.value = 'Укажите название'
    return
  }
  busy.value = true
  error.value = null
  try {
    const acc = await accounts.create({ name: form.name.trim(), challenge_type: form.challenge_type, size: form.size, stage: form.stage })
    if (accounts.list.length === 1) accounts.select(acc.id)
    form.name = ''
    showCreate.value = false
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <div class="space-y-3">
    <div class="flex flex-wrap items-center justify-between gap-2">
      <h1 class="text-lg font-semibold">Счета</h1>
      <div class="flex gap-2">
        <button type="button" class="btn btn-sm" :disabled="accounts.loading" @click="accounts.load()">Обновить</button>
        <button type="button" class="btn btn-sm btn-primary" data-testid="new-account" @click="showCreate = !showCreate">+ Новый счёт</button>
      </div>
    </div>

    <form v-if="showCreate" class="card grid grid-cols-2 gap-3 p-3 sm:grid-cols-5" data-testid="account-form" @submit.prevent="create">
      <div class="col-span-2">
        <label class="label" for="acc-name">Название</label>
        <input id="acc-name" v-model="form.name" class="input w-full" maxlength="60" required data-testid="acc-name" />
      </div>
      <div>
        <label class="label" for="acc-type">Тип</label>
        <select id="acc-type" v-model="form.challenge_type" class="input w-full" data-testid="acc-type">
          <option value="two_stage">{{ CHALLENGE_RU.two_stage }}</option>
          <option value="one_stage">{{ CHALLENGE_RU.one_stage }}</option>
        </select>
      </div>
      <div>
        <label class="label" for="acc-size">Размер</label>
        <select id="acc-size" v-model.number="form.size" class="input w-full" data-testid="acc-size">
          <option v-for="s in sizes" :key="s" :value="s">{{ fmtUsd(s) }}</option>
        </select>
      </div>
      <div>
        <label class="label" for="acc-stage">Стадия</label>
        <select id="acc-stage" v-model="form.stage" class="input w-full">
          <option v-for="s in stages" :key="s" :value="s">{{ STAGE_RU[s] }}</option>
        </select>
      </div>
      <div class="col-span-2 flex items-center gap-2 sm:col-span-5">
        <span v-if="error" class="text-sm text-short" role="alert">{{ error }}</span>
        <div class="ml-auto flex gap-2">
          <button type="button" class="btn btn-sm" @click="showCreate = false">Отмена</button>
          <button type="submit" class="btn btn-sm btn-primary" :disabled="busy" data-testid="acc-submit">Создать</button>
        </div>
      </div>
    </form>

    <p v-if="accounts.error" class="text-short" role="alert">{{ accounts.error }}</p>
    <p v-if="accounts.loaded && !accounts.list.length" class="card p-6 text-center text-muted">Счетов пока нет. Создайте первый челлендж.</p>
    <p v-else-if="!accounts.loaded" class="text-muted">Загрузка…</p>

    <div class="grid grid-cols-1 gap-3 2xl:grid-cols-2">
      <AccountCard v-for="a in accounts.list" :key="a.id" :account="a" />
    </div>
    <p class="text-muted-sm">
      Equity и запас до лимитов считаются по mark price и введённым позициям (дашборд HashHedge обновляется не в реальном времени). Встречные
      позиции между счетами запрещены.
    </p>
  </div>
</template>
