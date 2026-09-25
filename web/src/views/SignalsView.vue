<script setup lang="ts">
import { computed, onMounted, reactive, ref, shallowRef, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { api } from '@/api/endpoints'
import { errorMessage } from '@/api/client'
import type { CalibrationLive, Group, Side, Signal, SignalStatus } from '@/api/types'
import { usePrefsStore } from '@/stores/prefs'
import { useScreenerStore } from '@/stores/screener'
import { useInstrumentsStore } from '@/stores/instruments'
import ProbCell from '@/components/ProbCell.vue'
import CalibrationChart from '@/components/CalibrationChart.vue'
import { fmtDateTime, fmtNum, fmtPrice, fmtR } from '@/utils/format'
import { signClass } from '@/utils/colors'
import { GROUP_RU, SIDE_RU, SIGNAL_STATUS_RU } from '@/utils/labels'
import { dayEndExclusiveIso, dayStartIso } from '@/utils/dates'

const prefs = usePrefsStore()
const screener = useScreenerStore()
const instruments = useInstrumentsStore()
void instruments.load()
const route = useRoute()
const router = useRouter()

const STATUSES = Object.keys(SIGNAL_STATUS_RU) as SignalStatus[]
/** Группы статусов — уходят в API через запятую. */
const STATUS_GROUPS = {
  open: { label: 'открытые (новый/активен/вход)', statuses: ['new', 'active', 'triggered'] },
  closed: { label: 'с исходом (TP/SL/таймаут)', statuses: ['tp', 'sl', 'timeout'] },
} satisfies Record<string, { label: string; statuses: SignalStatus[] }>
type StatusGroup = keyof typeof STATUS_GROUPS
type StatusFilter = SignalStatus | StatusGroup | ''
const isGroup = (v: string): v is StatusGroup => Object.hasOwn(STATUS_GROUPS, v)
const q = (k: string) => (typeof route.query[k] === 'string' ? (route.query[k] as string) : '')
const isStatusFilter = (v: string): v is StatusFilter => v === '' || isGroup(v) || STATUSES.includes(v as SignalStatus)
/** Значение фильтра → параметр status API (группа — список через запятую). */
function statusParam(v: StatusFilter): SignalStatus | SignalStatus[] | '' {
  return isGroup(v) ? STATUS_GROUPS[v].statuses : v
}

const filters = reactive({
  status: (isStatusFilter(q('status')) ? q('status') : '') as StatusFilter,
  symbol: q('symbol'),
  side: (q('side') === 'long' || q('side') === 'short' ? q('side') : '') as Side | '',
  from: q('from'),
  to: q('to'),
})
const limit = ref(Number(q('limit')) || 50)
const offset = ref(Number(q('offset')) || 0)

const items = shallowRef<Signal[]>([])
const total = ref(0)
const loading = ref(false)
const error = ref<string | null>(null)
const expanded = ref<Set<number>>(new Set())
const deciding = ref<Set<number>>(new Set())

async function load(): Promise<void> {
  loading.value = true
  try {
    const res = await api.signals({
      status: statusParam(filters.status),
      symbol: filters.symbol.trim().toUpperCase() || undefined,
      side: filters.side,
      from: filters.from ? dayStartIso(filters.from, prefs.tz) : undefined,
      to: filters.to ? dayEndExclusiveIso(filters.to, prefs.tz) : undefined,
      limit: limit.value,
      offset: offset.value,
    })
    items.value = res.items
    total.value = res.total
    error.value = null
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    loading.value = false
  }
}

function syncQuery(): void {
  const query: Record<string, string> = {}
  if (filters.status) query.status = filters.status
  if (filters.symbol) query.symbol = filters.symbol
  if (filters.side) query.side = filters.side
  if (filters.from) query.from = filters.from
  if (filters.to) query.to = filters.to
  if (limit.value !== 50) query.limit = String(limit.value)
  if (offset.value) query.offset = String(offset.value)
  void router.replace({ query })
}

let debounce: ReturnType<typeof setTimeout> | null = null
watch(
  () => ({ ...filters }),
  () => {
    if (debounce) clearTimeout(debounce)
    debounce = setTimeout(() => {
      if (offset.value !== 0) {
        offset.value = 0 // загрузку выполнит watcher offset
        return
      }
      syncQuery()
      void load()
    }, 250)
  },
)
watch([limit, offset, () => prefs.tz], () => {
  syncQuery()
  void load()
})
onMounted(load)

// live: новые сигналы из WS — обновить первую страницу
watch(
  () => screener.lastSignal,
  () => {
    if (offset.value === 0 && !loading.value) void load()
  },
)

function resetFilters(): void {
  Object.assign(filters, { status: '', symbol: '', side: '', from: '', to: '' })
}

async function decide(s: Signal, taken: boolean): Promise<void> {
  deciding.value = new Set(deciding.value).add(s.id)
  try {
    const updated = await api.decision(s.id, taken)
    items.value = items.value.map((x) => (x.id === updated.id ? updated : x))
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    const next = new Set(deciding.value)
    next.delete(s.id)
    deciding.value = next
  }
}

function toggle(id: number): void {
  const next = new Set(expanded.value)
  if (next.has(id)) next.delete(id)
  else next.add(id)
  expanded.value = next
}

function statusClass(s: SignalStatus): string {
  if (s === 'tp') return 'badge-long'
  if (s === 'sl') return 'badge-short'
  if (s === 'new' || s === 'active' || s === 'triggered') return 'badge-accent'
  return 'badge-gray'
}

const page = computed(() => Math.floor(offset.value / limit.value) + 1)
const pages = computed(() => Math.max(1, Math.ceil(total.value / limit.value)))

// ---- калибровка ----
const calib = shallowRef<CalibrationLive | null>(null)
const calibGroup = ref<Group | ''>('')
const calibSide = ref<Side | ''>('')
const calibError = ref<string | null>(null)
async function loadCalib(): Promise<void> {
  try {
    calib.value = await api.calibrationLive(calibGroup.value, calibSide.value)
    calibError.value = null
  } catch (e) {
    calibError.value = errorMessage(e)
  }
}
watch([calibGroup, calibSide], loadCalib, { immediate: true })
</script>

<template>
  <div class="space-y-3">
    <h1 class="text-lg font-semibold">Журнал сигналов</h1>

    <div class="grid grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1fr)_380px]">
      <section class="min-w-0 space-y-2">
        <form class="card flex flex-wrap items-end gap-3 p-3" aria-label="Фильтры журнала" @submit.prevent>
          <div>
            <label class="label" for="sf-status">Статус</label>
            <select id="sf-status" v-model="filters.status" class="input" data-testid="sf-status">
              <option value="">все</option>
              <option v-for="(g, key) in STATUS_GROUPS" :key="key" :value="key">{{ g.label }}</option>
              <option v-for="s in STATUSES" :key="s" :value="s">{{ SIGNAL_STATUS_RU[s] }}</option>
            </select>
          </div>
          <div class="w-36">
            <label class="label" for="sf-symbol">Символ</label>
            <input id="sf-symbol" v-model="filters.symbol" class="input w-full" list="sf-symbols" placeholder="BTCUSDT" data-testid="sf-symbol" />
            <datalist id="sf-symbols">
              <option v-for="i in instruments.list" :key="i.hh_symbol" :value="i.hh_symbol" />
            </datalist>
          </div>
          <div>
            <label class="label" for="sf-side">Сторона</label>
            <select id="sf-side" v-model="filters.side" class="input" data-testid="sf-side">
              <option value="">обе</option>
              <option value="long">Лонг</option>
              <option value="short">Шорт</option>
            </select>
          </div>
          <div>
            <label class="label" for="sf-from">С ({{ prefs.tz === 'utc' ? 'UTC' : 'лок.' }})</label>
            <input id="sf-from" v-model="filters.from" type="date" class="input" />
          </div>
          <div>
            <label class="label" for="sf-to">По (включительно)</label>
            <input id="sf-to" v-model="filters.to" type="date" class="input" />
          </div>
          <button type="button" class="btn btn-sm" @click="resetFilters">Сбросить</button>
        </form>

        <p v-if="error" class="text-short" role="alert">{{ error }}</p>

        <div class="card overflow-x-auto" :class="loading ? 'opacity-60' : ''" :aria-busy="loading">
          <table class="tbl text-[13px]" data-testid="signals-table">
            <thead>
              <tr>
                <th class="left">Время</th>
                <th class="left">Инструмент</th>
                <th class="left">Сторона</th>
                <th>Score</th>
                <th>P</th>
                <th>EV</th>
                <th>Вход</th>
                <th>SL</th>
                <th>TP1 / TP2</th>
                <th class="left">Статус</th>
                <th>Исход</th>
                <th class="center">Решение</th>
                <th />
              </tr>
            </thead>
            <tbody>
              <template v-for="s in items" :key="s.id">
                <tr data-testid="signal-row" :data-signal-id="s.id">
                  <td class="left num">{{ fmtDateTime(s.created_at, prefs.tz, { year: false }) }}</td>
                  <td class="left">
                    <RouterLink :to="`/instrument/${s.hh_symbol}`" class="font-semibold hover:text-accent">{{ s.hh_symbol }}</RouterLink>
                    <span class="ml-1 text-muted">{{ s.tf }}</span>
                  </td>
                  <td class="left" :class="s.side === 'long' ? 'text-long' : 'text-short'">{{ SIDE_RU[s.side] }}</td>
                  <td class="num">{{ fmtNum(s.score, 0) }}</td>
                  <td><ProbCell :prob="s.prob" :side="s.side" :score="s.score" /></td>
                  <td class="num" :class="signClass(s.ev_r)">{{ fmtR(s.ev_r) }}</td>
                  <td class="num">{{ fmtPrice(s.entry) }} <span class="text-muted">{{ s.entry_type }}</span></td>
                  <td class="num">{{ fmtPrice(s.sl) }} <span class="text-muted">{{ s.sl_trigger }}</span></td>
                  <td class="num">{{ fmtPrice(s.tp1) }} / {{ fmtPrice(s.tp2) }}</td>
                  <td class="left"><span class="badge" :class="statusClass(s.status)" data-field="status">{{ SIGNAL_STATUS_RU[s.status] }}</span></td>
                  <td class="num font-semibold" :class="signClass(s.outcome_r)" data-field="outcome">{{ fmtR(s.outcome_r) }}</td>
                  <td class="center">
                    <div class="inline-flex gap-1" role="group" aria-label="Решение по сигналу">
                      <button
                        type="button"
                        class="btn btn-sm"
                        :class="s.taken === true ? 'btn-active' : ''"
                        :aria-pressed="s.taken === true"
                        :disabled="deciding.has(s.id)"
                        data-testid="took"
                        @click="decide(s, true)"
                      >
                        Взял
                      </button>
                      <button
                        type="button"
                        class="btn btn-sm"
                        :class="s.taken === false ? 'btn-active' : ''"
                        :aria-pressed="s.taken === false"
                        :disabled="deciding.has(s.id)"
                        data-testid="skipped"
                        @click="decide(s, false)"
                      >
                        Пропустил
                      </button>
                    </div>
                  </td>
                  <td>
                    <button type="button" class="btn btn-sm px-1.5" :aria-expanded="expanded.has(s.id)" :title="expanded.has(s.id) ? 'Свернуть' : 'Подробнее'" @click="toggle(s.id)">
                      {{ expanded.has(s.id) ? '▴' : '▾' }}
                    </button>
                  </td>
                </tr>
                <tr v-if="expanded.has(s.id)">
                  <td colspan="13" class="left whitespace-normal bg-surface-2">
                    <div class="grid grid-cols-1 gap-2 py-1 text-[13px] sm:grid-cols-3">
                      <div>
                        <div class="font-semibold text-long">За</div>
                        <ul class="list-disc pl-5"><li v-for="(p, i) in s.reason.pros" :key="i">{{ p }}</li></ul>
                      </div>
                      <div>
                        <div class="font-semibold text-short">Против</div>
                        <ul class="list-disc pl-5"><li v-for="(p, i) in s.reason.cons" :key="i">{{ p }}</li></ul>
                      </div>
                      <div class="num text-muted">
                        #{{ s.id }} · {{ s.strategy }} · бар {{ fmtDateTime(s.bar_ts, prefs.tz, { year: false }) }}<br />
                        действует до {{ fmtDateTime(s.expires_at, prefs.tz, { year: false }) }}<br />
                        <template v-if="s.closed_at">закрыт {{ fmtDateTime(s.closed_at, prefs.tz, { year: false }) }}</template>
                      </div>
                    </div>
                  </td>
                </tr>
              </template>
              <tr v-if="!items.length">
                <td colspan="13" class="center py-8 text-muted">{{ loading ? 'Загрузка…' : 'Нет сигналов под фильтры' }}</td>
              </tr>
            </tbody>
          </table>
        </div>

        <nav class="flex flex-wrap items-center gap-2 text-sm" aria-label="Страницы">
          <span class="num text-muted" data-testid="signals-range">
            {{ total ? offset + 1 : 0 }}–{{ Math.min(offset + limit, total) }} из <span data-testid="signals-total">{{ total }}</span>
          </span>
          <button type="button" class="btn btn-sm" :disabled="offset === 0" data-testid="page-prev" @click="offset = Math.max(0, offset - limit)">← Назад</button>
          <span class="num">{{ page }} / {{ pages }}</span>
          <button type="button" class="btn btn-sm" :disabled="offset + limit >= total" data-testid="page-next" @click="offset += limit">Вперёд →</button>
          <label class="ml-auto text-muted" for="sf-limit">На странице</label>
          <select id="sf-limit" v-model.number="limit" class="input py-0.5">
            <option :value="25">25</option>
            <option :value="50">50</option>
            <option :value="100">100</option>
          </select>
        </nav>
      </section>

      <section class="card h-fit space-y-2 p-3">
        <h2 class="font-semibold">Калибровка live</h2>
        <div class="flex flex-wrap gap-2">
          <select v-model="calibGroup" class="input py-0.5 text-sm" aria-label="Группа">
            <option value="">все группы</option>
            <option v-for="g in (['crypto_major', 'crypto_alt', 'tradfi'] as Group[])" :key="g" :value="g">{{ GROUP_RU[g] }}</option>
          </select>
          <select v-model="calibSide" class="input py-0.5 text-sm" aria-label="Сторона">
            <option value="">обе стороны</option>
            <option value="long">Лонг</option>
            <option value="short">Шорт</option>
          </select>
        </div>
        <p v-if="calibError" class="text-short" role="alert">{{ calibError }}</p>
        <CalibrationChart v-if="calib" :data="calib" />
        <p class="text-muted-sm">
          Точки — корзины по предсказанной P, размер — число сигналов, вертикаль — ДИ Wilson 95%. Пунктир — идеальная калибровка. Исходы
          отслеживаются и для невзятых сигналов.
        </p>
      </section>
    </div>
  </div>
</template>
