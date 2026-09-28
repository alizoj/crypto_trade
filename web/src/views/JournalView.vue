<script setup lang="ts">
// Журнал сделок: Элдер — таблица всех сделок и дневник; Дуглас — оценка сериями по 20 сделок и контроль своих ошибок.
import { computed, onMounted, reactive, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/endpoints'
import { errorMessage } from '@/api/client'
import type { ExitReason, JournalBlock, JournalStats, JournalTrade } from '@/api/types'
import { useAccountsStore } from '@/stores/accounts'
import { usePrefsStore } from '@/stores/prefs'
import { fmtDateTime, fmtNum, fmtPrice, fmtQty, fmtR, fmtShare, fmtUsd } from '@/utils/format'
import { signClass } from '@/utils/colors'
import { EXIT_REASON_RU, SIDE_RU } from '@/utils/labels'
import { dayEndExclusiveIso, dayStartIso } from '@/utils/dates'

const LIMIT = 100
const EXIT_REASONS = Object.keys(EXIT_REASON_RU) as ExitReason[]

const prefs = usePrefsStore()
const accounts = useAccountsStore()
if (!accounts.loaded) void accounts.load()
const route = useRoute()
const router = useRouter()
const q = (k: string) => (typeof route.query[k] === 'string' ? (route.query[k] as string) : '')

const filters = reactive({ account: q('account'), from: q('from'), to: q('to') })
const offset = ref(0)
const items = shallowRef<JournalTrade[]>([])
const total = ref(0)
const stats = shallowRef<JournalStats | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
const editingId = ref<number | null>(null)
const noteInput = ref('')
const saving = ref<Set<number>>(new Set())

const accountName = computed(() => new Map(accounts.list.map((a) => [a.id, a.name])))

function query() {
  return {
    account_id: filters.account ? Number(filters.account) : null,
    from: filters.from ? dayStartIso(filters.from, prefs.tz) : undefined,
    to: filters.to ? dayEndExclusiveIso(filters.to, prefs.tz) : undefined,
  }
}

async function load(): Promise<void> {
  loading.value = true
  try {
    const [list, st] = await Promise.all([api.journal({ ...query(), limit: LIMIT, offset: offset.value }), api.journalStats(query())])
    items.value = list.items
    total.value = list.total
    stats.value = st
    error.value = null
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    loading.value = false
  }
}

watch(
  () => ({ ...filters }),
  () => {
    const query: Record<string, string> = {}
    if (filters.account) query.account = filters.account
    if (filters.from) query.from = filters.from
    if (filters.to) query.to = filters.to
    void router.replace({ query })
    offset.value = 0
    void load()
  },
)
watch([offset, () => prefs.tz], () => void load())
onMounted(load)

const current = computed(() => stats.value?.series.at(-1) ?? null)
const previous = computed(() => (stats.value?.series.slice(0, -1) ?? []).slice().reverse())
const seriesSize = computed(() => stats.value?.series_size ?? 20)

function replaceItem(t: JournalTrade): void {
  items.value = items.value.map((x) => (x.id === t.id ? t : x))
}

async function patch(t: JournalTrade, body: { note?: string; exit_reason?: ExitReason }): Promise<boolean> {
  saving.value = new Set(saving.value).add(t.id)
  try {
    replaceItem(await api.patchJournal(t.id, body))
    return true
  } catch (e) {
    error.value = errorMessage(e)
    return false
  } finally {
    const next = new Set(saving.value)
    next.delete(t.id)
    saving.value = next
  }
}

function startNote(t: JournalTrade): void {
  editingId.value = t.id
  noteInput.value = t.note
}

async function saveNote(t: JournalTrade): Promise<void> {
  if (await patch(t, { note: noteInput.value.trim() })) editingId.value = null
}

function setReason(t: JournalTrade, e: Event): void {
  void patch(t, { exit_reason: (e.target as HTMLSelectElement).value as ExitReason })
}

function pf(b: JournalBlock): string {
  return b.profit_factor === null ? '—' : fmtNum(b.profit_factor, 2)
}

function resetFilters(): void {
  Object.assign(filters, { account: '', from: '', to: '' })
}
</script>

<template>
  <div class="space-y-3">
    <div class="flex flex-wrap items-baseline justify-between gap-2">
      <h1 class="text-lg font-semibold">Журнал сделок</h1>
      <p class="text-muted-sm">
        Элдер: записывайте каждую сделку и причину выхода. Дуглас: судите о системе по серии из {{ seriesSize }} сделок, а не по последней.
      </p>
    </div>

    <form class="card flex flex-wrap items-end gap-3 p-3" aria-label="Фильтры журнала сделок" @submit.prevent>
      <div>
        <label class="label" for="jf-account">Счёт</label>
        <select id="jf-account" v-model="filters.account" class="input" data-testid="jf-account">
          <option value="">все счета</option>
          <option v-for="a in accounts.list" :key="a.id" :value="String(a.id)">{{ a.name }}</option>
        </select>
      </div>
      <div>
        <label class="label" for="jf-from">Закрыта с ({{ prefs.tz === 'utc' ? 'UTC' : 'лок.' }})</label>
        <input id="jf-from" v-model="filters.from" type="date" class="input" />
      </div>
      <div>
        <label class="label" for="jf-to">По (включительно)</label>
        <input id="jf-to" v-model="filters.to" type="date" class="input" />
      </div>
      <button type="button" class="btn btn-sm" @click="resetFilters">Сбросить</button>
      <button type="button" class="btn btn-sm" :disabled="loading" @click="load">Обновить</button>
    </form>

    <p v-if="error" class="text-short" role="alert">{{ error }}</p>

    <div v-if="stats && stats.total.n" class="grid grid-cols-1 gap-3 lg:grid-cols-2">
      <section v-if="current" class="card space-y-2 p-3" data-testid="current-series">
        <div class="flex items-baseline justify-between gap-2">
          <h2 class="text-sm font-semibold">Серия {{ current.index }}</h2>
          <b class="num" data-field="series-progress">{{ current.n }}/{{ seriesSize }}</b>
        </div>
        <div class="h-2 w-full overflow-hidden rounded-full bg-surface-2" role="meter" :aria-valuenow="current.n" aria-valuemin="0" :aria-valuemax="seriesSize">
          <div class="h-full rounded-full bg-accent" :style="{ width: `${(current.n / seriesSize) * 100}%` }" />
        </div>
        <p v-if="!current.complete" class="text-muted-sm">
          Вывод — после {{ seriesSize }} сделок. До конца серии правила не меняйте: по нескольким сделкам нельзя отличить неудачу от сломанной
          системы.
        </p>
        <div class="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <div>
            <div class="text-muted-sm">Прибыльных</div>
            <b class="num">{{ current.wins }}/{{ current.n }}</b>
            <div class="num text-xs text-muted">{{ fmtShare(current.win_rate) }}</div>
          </div>
          <div>
            <div class="text-muted-sm">Средний результат</div>
            <b class="num" :class="signClass(current.avg_r)" data-field="series-avg-r">{{ fmtR(current.avg_r) }}</b>
            <div class="num text-xs text-muted">
              ожидание {{ fmtR(current.expected_avg_r) }}<template v-if="current.n_expected"> ({{ current.n_expected }} сд.)</template>
            </div>
          </div>
          <div>
            <div class="text-muted-sm">Итог серии</div>
            <b class="num" :class="signClass(current.pnl_usd)">{{ fmtUsd(current.pnl_usd, 2, true) }}</b>
            <div class="num text-xs text-muted">комиссии {{ fmtUsd(current.fees_usd, 2) }}</div>
          </div>
          <div>
            <div class="text-muted-sm">Убытков подряд</div>
            <b class="num">{{ current.max_loss_streak }}</b>
          </div>
        </div>
        <div class="border-t border-line pt-2">
          <div class="text-muted-sm">Дисциплина (Дуглас: следить за своими ошибками)</div>
          <div class="flex flex-wrap gap-x-4 gap-y-1 text-sm" data-testid="discipline">
            <span>по сигналам: <b class="num">{{ current.from_signals }}/{{ current.n }}</b></span>
            <span :class="current.without_stop ? 'text-warn' : ''">без стопа: <b class="num">{{ current.without_stop }}</b></span>
            <span :class="current.stop_breaches ? 'text-short' : ''" title="Убыток больше 1.1R — стоп перенесён или не стоял"
              >убыток больше риска: <b class="num">{{ current.stop_breaches }}</b></span
            >
          </div>
        </div>
      </section>

      <section class="card space-y-2 p-3" data-testid="journal-total">
        <h2 class="text-sm font-semibold">Всего</h2>
        <div class="grid grid-cols-2 gap-2 sm:grid-cols-4">
          <div>
            <div class="text-muted-sm">Сделок</div>
            <b class="num" data-field="total-n">{{ stats.total.n }}</b>
            <div class="num text-xs text-muted">с R: {{ stats.total.n_r }}</div>
          </div>
          <div>
            <div class="text-muted-sm">Результат</div>
            <b class="num" :class="signClass(stats.total.pnl_usd)">{{ fmtUsd(stats.total.pnl_usd, 2, true) }}</b>
            <div class="num text-xs text-muted">сумма {{ fmtR(stats.total.sum_r) }}</div>
          </div>
          <div>
            <div class="text-muted-sm">Средний / PF</div>
            <b class="num" :class="signClass(stats.total.avg_r)">{{ fmtR(stats.total.avg_r) }}</b>
            <div class="num text-xs text-muted">PF {{ pf(stats.total) }} · {{ fmtShare(stats.total.win_rate) }} прибыльных</div>
          </div>
          <div>
            <div class="text-muted-sm">Средняя прибыль / убыток</div>
            <b class="num">{{ fmtR(stats.total.avg_win_r) }} / {{ fmtR(stats.total.avg_loss_r) }}</b>
          </div>
        </div>
        <div v-if="previous.length" class="overflow-x-auto border-t border-line pt-2">
          <table class="tbl text-[13px]" data-testid="series-table">
            <thead>
              <tr>
                <th class="left">Серия</th>
                <th class="left">Период</th>
                <th>Прибыльных</th>
                <th>Средний</th>
                <th>Ожидание</th>
                <th>Итог</th>
                <th>Убытков подряд</th>
                <th>Нарушения</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="s in previous" :key="s.index" data-testid="series-row">
                <td class="left">{{ s.index }}</td>
                <td class="left num">
                  {{ fmtDateTime(s.first_closed_at, prefs.tz, { year: false }) }} —
                  {{ fmtDateTime(s.last_closed_at, prefs.tz, { year: false }) }}
                </td>
                <td class="num">{{ s.wins }}/{{ s.n }}</td>
                <td class="num" :class="signClass(s.avg_r)">{{ fmtR(s.avg_r) }}</td>
                <td class="num">{{ fmtR(s.expected_avg_r) }}</td>
                <td class="num" :class="signClass(s.pnl_usd)">{{ fmtUsd(s.pnl_usd, 0, true) }}</td>
                <td class="num">{{ s.max_loss_streak }}</td>
                <td class="num" :class="s.stop_breaches + s.without_stop ? 'text-short' : ''">{{ s.stop_breaches + s.without_stop }}</td>
              </tr>
            </tbody>
          </table>
        </div>
      </section>
    </div>

    <p v-else-if="stats && !loading" class="card p-6 text-center text-muted" data-testid="journal-empty">
      Закрытых сделок пока нет. Сделка попадает сюда при закрытии позиции на экране «Счета» или командой бота /pos close.
    </p>

    <div v-if="items.length" class="card overflow-x-auto" :class="loading ? 'opacity-60' : ''" :aria-busy="loading">
      <table class="tbl text-[13px]" data-testid="journal-table">
        <thead>
          <tr>
            <th class="left">Закрыта</th>
            <th class="left">Счёт</th>
            <th class="left">Инструмент</th>
            <th class="left">Сторона</th>
            <th>Кол-во</th>
            <th>Вход → выход</th>
            <th>SL</th>
            <th>Результат</th>
            <th>R</th>
            <th class="left">Причина</th>
            <th class="left">Заметка</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="t in items" :key="t.id" data-testid="journal-row" :data-trade-id="t.id">
            <td class="left num">{{ fmtDateTime(t.closed_at, prefs.tz, { year: false }) }}</td>
            <td class="left">{{ accountName.get(t.account_id) ?? `#${t.account_id}` }}</td>
            <td class="left">
              <RouterLink :to="`/instrument/${t.hh_symbol}`" class="font-semibold hover:text-accent">{{ t.hh_symbol }}</RouterLink>
              <span v-if="t.signal_id !== null" class="ml-1 badge badge-accent" :title="t.strategy">сигнал #{{ t.signal_id }}</span>
            </td>
            <td class="left" :class="t.side === 'long' ? 'text-long' : 'text-short'">{{ SIDE_RU[t.side] }}</td>
            <td class="num">{{ fmtQty(t.qty) }}</td>
            <td class="num">{{ fmtPrice(t.entry_price) }} → {{ fmtPrice(t.exit_price) }}</td>
            <td class="num" :class="t.sl === null ? 'text-warn' : ''">{{ t.sl === null ? 'нет' : fmtPrice(t.sl) }}</td>
            <td class="num font-semibold" :class="signClass(t.pnl_usd)" data-field="pnl">{{ fmtUsd(t.pnl_usd, 2, true) }}</td>
            <td class="num" :class="[signClass(t.r_multiple), t.r_multiple !== null && t.r_multiple < -1.1 ? 'underline decoration-dotted' : '']">
              {{ fmtR(t.r_multiple) }}
            </td>
            <td class="left">
              <select
                class="input py-0.5 text-sm"
                :value="t.exit_reason"
                :disabled="saving.has(t.id)"
                aria-label="Причина выхода"
                data-testid="journal-reason"
                @change="setReason(t, $event)"
              >
                <option v-for="r in EXIT_REASONS" :key="r" :value="r">{{ EXIT_REASON_RU[r] }}</option>
              </select>
            </td>
            <td class="left max-w-[320px]">
              <form v-if="editingId === t.id" class="flex items-center gap-1" @submit.prevent="saveNote(t)">
                <input v-model="noteInput" class="input w-56 py-0.5 text-sm" maxlength="4000" aria-label="Заметка" data-testid="note-input" autofocus />
                <button type="submit" class="btn btn-sm btn-primary" :disabled="saving.has(t.id)" data-testid="note-save">OK</button>
                <button type="button" class="btn btn-sm" @click="editingId = null">✕</button>
              </form>
              <button v-else type="button" class="text-left hover:text-accent" data-testid="note-edit" @click="startNote(t)">
                <span v-if="t.note" class="whitespace-normal">{{ t.note }}</span>
                <span v-else class="text-muted">+ заметка</span>
              </button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <div v-if="total > LIMIT" class="flex items-center justify-end gap-2 text-sm">
      <span class="text-muted num">{{ offset + 1 }}–{{ Math.min(offset + LIMIT, total) }} из {{ total }}</span>
      <button type="button" class="btn btn-sm" :disabled="offset === 0" @click="offset = Math.max(0, offset - LIMIT)">←</button>
      <button type="button" class="btn btn-sm" :disabled="offset + LIMIT >= total" @click="offset += LIMIT">→</button>
    </div>
  </div>
</template>
