<script setup lang="ts">
import { computed, ref } from 'vue'
import type { Account, AccountStatus, PositionCreate, Stage } from '@/api/types'
import { errorMessage } from '@/api/client'
import { useAccountsStore } from '@/stores/accounts'
import { usePrefsStore } from '@/stores/prefs'
import RoomBar from '@/components/RoomBar.vue'
import PositionForm from './PositionForm.vue'
import { fmtDateTime, fmtNum, fmtPct, fmtPrice, fmtQty, fmtUsd } from '@/utils/format'
import { signClass } from '@/utils/colors'
import { ACCOUNT_STATUS_RU, CHALLENGE_RU, SIDE_RU, STAGE_RU } from '@/utils/labels'

const props = defineProps<{ account: Account }>()

const accounts = useAccountsStore()
const prefs = usePrefsStore()

const a = computed(() => props.account)
const isSelected = computed(() => accounts.selected?.id === a.value.id)
const busy = ref(false)
const error = ref<string | null>(null)
const editingBalance = ref(false)
const balanceInput = ref('')
const showPosForm = ref(false)
const closingId = ref<number | null>(null)
const exitInput = ref('')
const confirmDelete = ref(false)

const unrealized = computed(() => a.value.equity - a.value.balance)
const progress = computed(() => (a.value.progress_pct === null ? null : Math.max(0, Math.min(100, a.value.progress_pct))))
const stages = computed<Stage[]>(() => (a.value.challenge_type === 'one_stage' ? ['stage1', 'trader'] : ['stage1', 'stage2', 'trader']))

async function run(fn: () => Promise<unknown>): Promise<boolean> {
  busy.value = true
  error.value = null
  try {
    await fn()
    return true
  } catch (e) {
    error.value = errorMessage(e)
    return false
  } finally {
    busy.value = false
  }
}

function startBalance(): void {
  balanceInput.value = String(a.value.balance)
  editingBalance.value = true
}

async function saveBalance(): Promise<void> {
  const v = Number(balanceInput.value.replace(',', '.').replace(/\s/g, ''))
  if (!Number.isFinite(v) || v <= 0) {
    error.value = 'Баланс должен быть положительным числом'
    return
  }
  if (await run(() => accounts.patch(a.value.id, { balance: v }))) editingBalance.value = false
}

function setStage(e: Event): void {
  void run(() => accounts.patch(a.value.id, { stage: (e.target as HTMLSelectElement).value as Stage }))
}

function setStatus(e: Event): void {
  void run(() => accounts.patch(a.value.id, { status: (e.target as HTMLSelectElement).value as AccountStatus }))
}

async function addPosition(body: PositionCreate): Promise<void> {
  if (await run(() => accounts.addPosition(a.value.id, body))) showPosForm.value = false
}

function startClose(posId: number, mark: number | null): void {
  closingId.value = posId
  exitInput.value = mark === null ? '' : String(mark)
}

async function confirmClose(): Promise<void> {
  if (closingId.value === null) return
  const raw = exitInput.value.trim().replace(',', '.')
  const exit = raw === '' ? undefined : Number(raw)
  if (exit !== undefined && (!Number.isFinite(exit) || exit <= 0)) {
    error.value = 'Цена выхода должна быть положительным числом'
    return
  }
  const id = closingId.value
  if (await run(() => accounts.closePosition(a.value.id, id, exit))) closingId.value = null
}

async function remove(): Promise<void> {
  await run(() => accounts.remove(a.value.id))
}
</script>

<template>
  <article
    class="card space-y-3 p-3"
    :class="a.target_lock ? 'ring-2 ring-warn' : a.soft_stop_hit ? 'ring-2 ring-short' : ''"
    data-testid="account-card"
    :data-account-id="a.id"
  >
    <header class="flex flex-wrap items-start justify-between gap-2">
      <div class="min-w-0">
        <h2 class="truncate text-base font-semibold">{{ a.name }}</h2>
        <p class="text-muted-sm num">
          {{ CHALLENGE_RU[a.challenge_type] }} · {{ fmtUsd(a.size) }} · {{ STAGE_RU[a.stage] }} · {{ ACCOUNT_STATUS_RU[a.status] }} ·
          обновлён {{ fmtDateTime(a.updated_at, prefs.tz, { year: false }) }}
        </p>
      </div>
      <div class="flex flex-wrap items-center gap-1">
        <span v-if="a.target_lock" class="badge badge-warn" data-testid="target-lock">target lock</span>
        <span v-if="a.soft_stop_hit" class="badge badge-short">soft stop</span>
        <span v-if="isSelected" class="badge badge-accent">текущий</span>
        <button v-else type="button" class="btn btn-sm" @click="accounts.select(a.id)">Сделать текущим</button>
      </div>
    </header>

    <p v-if="a.target_lock" class="rounded-md border border-warn/40 bg-warn/10 px-2 py-1.5 text-sm text-warn">
      Цель достигнута — режим добора торговых дней ({{ a.trading_days }}/{{ a.min_trading_days }}): минимальный риск, без новых целей по прибыли.
    </p>
    <p v-if="a.soft_stop_hit" class="rounded-md border border-short/40 bg-short/10 px-2 py-1.5 text-sm text-short">
      Soft daily stop: новые входы заблокированы до ресета.
    </p>

    <div class="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <div>
        <div class="text-muted-sm">Баланс</div>
        <div v-if="!editingBalance" class="flex items-center gap-1">
          <b class="num text-base" data-field="balance">{{ fmtUsd(a.balance, 2) }}</b>
          <button type="button" class="btn btn-sm px-1.5" title="Ввести баланс вручную" data-testid="edit-balance" @click="startBalance">✎</button>
        </div>
        <form v-else class="flex items-center gap-1" @submit.prevent="saveBalance">
          <input v-model="balanceInput" class="input num w-28 py-0.5" inputmode="decimal" aria-label="Баланс" data-testid="balance-input" autofocus />
          <button type="submit" class="btn btn-sm btn-primary" :disabled="busy" data-testid="balance-save">OK</button>
          <button type="button" class="btn btn-sm" @click="editingBalance = false">✕</button>
        </form>
      </div>
      <div>
        <div class="text-muted-sm">Equity (по mark)</div>
        <b class="num text-base" data-field="equity">{{ fmtUsd(a.equity, 2) }}</b>
        <div class="num text-xs" :class="signClass(unrealized)">{{ fmtUsd(unrealized, 2, true) }} нереализ.</div>
      </div>
      <div>
        <div class="text-muted-sm">Прогресс к цели</div>
        <template v-if="a.target_usd !== null">
          <b class="num text-base" data-field="progress">{{ fmtPct(a.progress_pct, 1) }}</b>
          <div class="mt-1 h-2 w-full overflow-hidden rounded-full bg-surface-2">
            <div class="h-full rounded-full bg-accent" :style="{ width: `${progress ?? 0}%` }" />
          </div>
          <div class="num text-xs text-muted">цель {{ fmtUsd(a.target_usd) }}</div>
        </template>
        <span v-else class="text-muted">нет цели</span>
      </div>
      <div>
        <div class="text-muted-sm">Торговые дни</div>
        <b class="num text-base" data-field="days" :class="a.trading_days >= a.min_trading_days ? 'text-long' : ''"
          >{{ a.trading_days }}/{{ a.min_trading_days }}</b
        >
      </div>
    </div>

    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <div>
        <div class="flex justify-between text-muted-sm">
          <span>Запас до дневного лимита</span>
          <b class="num text-fg" data-field="daily-room">{{ fmtUsd(a.daily_room_usd, 2) }}</b>
        </div>
        <RoomBar :room="a.daily_room_usd" :limit="a.daily_limit_usd" />
        <div class="num text-xs text-muted">
          floor {{ fmtUsd(a.daily_floor, 2) }} · старт дня {{ fmtUsd(a.start_of_day_balance, 2) }} · лимит <span data-field="daily-limit">{{ fmtUsd(a.daily_limit_usd) }}</span>
        </div>
      </div>
      <div>
        <div class="flex justify-between text-muted-sm">
          <span>Запас до общего лимита</span>
          <b class="num text-fg" data-field="total-room">{{ fmtUsd(a.total_room_usd, 2) }}</b>
        </div>
        <RoomBar :room="a.total_room_usd" :limit="a.total_limit_usd" />
        <div class="num text-xs text-muted">floor {{ fmtUsd(a.total_floor, 2) }} · лимит <span data-field="total-limit">{{ fmtUsd(a.total_limit_usd) }}</span></div>
      </div>
    </div>

    <div>
      <div class="mb-1 flex items-center justify-between gap-2">
        <h3 class="text-sm font-semibold">Открытые позиции ({{ a.positions.length }})</h3>
        <button v-if="!showPosForm" type="button" class="btn btn-sm" data-testid="add-position" @click="showPosForm = true">+ Позиция</button>
      </div>
      <PositionForm v-if="showPosForm" :busy="busy" @submit="addPosition" @cancel="showPosForm = false" />
      <div v-if="a.positions.length" class="overflow-x-auto">
        <table class="tbl text-[13px]">
          <thead>
            <tr>
              <th class="left">Инструмент</th>
              <th class="left">Сторона</th>
              <th>Кол-во</th>
              <th>Вход</th>
              <th>Mark</th>
              <th>Notional</th>
              <th>PnL</th>
              <th>SL / TP</th>
              <th class="left">Открыта</th>
              <th />
            </tr>
          </thead>
          <tbody>
            <tr v-for="p in a.positions" :key="p.id" data-testid="position-row">
              <td class="left">
                <RouterLink :to="`/instrument/${p.hh_symbol}`" class="font-semibold hover:text-accent">{{ p.hh_symbol }}</RouterLink>
              </td>
              <td class="left" :class="p.side === 'long' ? 'text-long' : 'text-short'">{{ SIDE_RU[p.side] }}</td>
              <td class="num">{{ fmtQty(p.qty) }}</td>
              <td class="num">{{ fmtPrice(p.entry_price) }}</td>
              <td class="num">{{ fmtPrice(p.mark_price) }}</td>
              <td class="num">{{ fmtUsd(p.notional_usd) }}</td>
              <td class="num" :class="signClass(p.unrealized_usd)">{{ fmtUsd(p.unrealized_usd, 2, true) }}</td>
              <td class="num">{{ fmtPrice(p.sl) }} / {{ fmtPrice(p.tp) }}</td>
              <td class="left num">{{ fmtDateTime(p.opened_at, prefs.tz, { year: false }) }}</td>
              <td>
                <form v-if="closingId === p.id" class="flex items-center gap-1" @submit.prevent="confirmClose">
                  <input v-model="exitInput" class="input num w-24 py-0.5" placeholder="цена выхода" aria-label="Цена выхода" />
                  <button type="submit" class="btn btn-sm btn-primary" :disabled="busy">Закрыть</button>
                  <button type="button" class="btn btn-sm" @click="closingId = null">✕</button>
                </form>
                <button v-else type="button" class="btn btn-sm" data-testid="close-position" @click="startClose(p.id, p.mark_price)">Закрыть</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p v-else-if="!showPosForm" class="text-muted-sm">Нет открытых позиций</p>
      <p class="mt-1 text-muted-sm num">
        Суммарный notional: {{ fmtUsd(a.positions.reduce((s, p) => s + p.notional_usd, 0)) }} · плечо
        {{ fmtNum(a.equity > 0 ? a.positions.reduce((s, p) => s + p.notional_usd, 0) / a.equity : 0, 2) }}× (макс. 5×)
      </p>
    </div>

    <footer class="flex flex-wrap items-center gap-2 border-t border-line pt-2">
      <label class="text-muted-sm" :for="`stage-${a.id}`">Стадия</label>
      <select :id="`stage-${a.id}`" class="input py-0.5 text-sm" :value="a.stage" :disabled="busy" @change="setStage">
        <option v-for="s in stages" :key="s" :value="s">{{ STAGE_RU[s] }}</option>
      </select>
      <label class="text-muted-sm" :for="`status-${a.id}`">Статус</label>
      <select :id="`status-${a.id}`" class="input py-0.5 text-sm" :value="a.status" :disabled="busy" @change="setStatus">
        <option value="active">{{ ACCOUNT_STATUS_RU.active }}</option>
        <option value="passed">{{ ACCOUNT_STATUS_RU.passed }}</option>
        <option value="failed">{{ ACCOUNT_STATUS_RU.failed }}</option>
      </select>
      <span v-if="error" class="text-sm text-short" role="alert">{{ error }}</span>
      <div class="ml-auto flex items-center gap-1">
        <template v-if="confirmDelete">
          <span class="text-sm">Удалить счёт?</span>
          <button type="button" class="btn btn-sm btn-short" :disabled="busy" @click="remove">Удалить</button>
          <button type="button" class="btn btn-sm" @click="confirmDelete = false">Нет</button>
        </template>
        <button v-else type="button" class="btn btn-sm" @click="confirmDelete = true">Удалить</button>
      </div>
    </footer>
  </article>
</template>
