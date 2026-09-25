<script setup lang="ts">
import { computed, ref, watch } from 'vue'
import {
  getCoreRowModel,
  getSortedRowModel,
  useVueTable,
  type ColumnDef,
  type SortingState,
  type VisibilityState,
} from '@tanstack/vue-table'
import type { ScreenerRow } from '@/api/types'
import { useScreenerStore } from '@/stores/screener'
import { usePrefsStore } from '@/stores/prefs'
import { useMediaQuery } from '@/composables/useMediaQuery'
import { SCREENER_COLUMNS, type ColId } from '@/components/screener/columns'
import ScreenerCell from '@/components/screener/ScreenerCell.vue'
import ScreenerFilters from '@/components/screener/ScreenerFilters.vue'
import ProbCell from '@/components/ProbCell.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import TrendIcon from '@/components/TrendIcon.vue'
import { fmtDateTime, fmtFunding, fmtNum, fmtPct, fmtPrice, fmtR, fmtUsdCompact } from '@/utils/format'
import { signClass, rsiClass } from '@/utils/colors'
import { ASSET_CLASS_RU } from '@/utils/labels'
import { STORAGE_KEYS, readJSON, writeJSON } from '@/utils/storage'

const screener = useScreenerStore()
const prefs = usePrefsStore()
const isDesktop = useMediaQuery('(min-width: 768px)')

const SORT_KEY = `${STORAGE_KEYS.screenerState}.sort`
const validIds = new Set<string>(SCREENER_COLUMNS.map((c) => c.id))
const sorting = ref<SortingState>(
  readJSON<SortingState>(SORT_KEY, [{ id: 'status', desc: false }], (v): v is SortingState =>
    Array.isArray(v) && v.every((s) => s && typeof s === 'object' && validIds.has((s as { id: string }).id)),
  ),
)
watch(sorting, (s) => writeJSON(SORT_KEY, s), { deep: true })

const columnVisibility = computed<VisibilityState>(() => {
  const side = screener.filters.side
  const vis: VisibilityState = {}
  for (const c of SCREENER_COLUMNS) vis[c.id] = !c.side || side === 'both' || c.side === side
  return vis
})

const columns: ColumnDef<ScreenerRow>[] = SCREENER_COLUMNS.map((c) => ({
  id: c.id,
  accessorFn: (r: ScreenerRow) => c.sortValue(r),
  header: c.header,
  sortUndefined: 'last',
  sortingFn: c.text ? 'text' : 'basic',
  sortDescFirst: !c.text && c.id !== 'status',
}))

const rows = computed(() => screener.filteredRows)

const table = useVueTable({
  get data() {
    return rows.value
  },
  columns,
  state: {
    get sorting() {
      return sorting.value
    },
    get columnVisibility() {
      return columnVisibility.value
    },
  },
  onSortingChange: (updater) => {
    sorting.value = typeof updater === 'function' ? updater(sorting.value) : updater
  },
  getCoreRowModel: getCoreRowModel(),
  getSortedRowModel: getSortedRowModel(),
  getRowId: (r) => r.hh_symbol,
  enableMultiSort: true,
  isMultiSortEvent: (e) => (e as MouseEvent).shiftKey,
})

const sortedRows = computed(() => {
  // зависимость от данных/сортировки для реактивного пересчёта
  void rows.value
  void sorting.value
  return table.getRowModel().rows.map((r) => r.original)
})

const colMeta = new Map(SCREENER_COLUMNS.map((c) => [c.id, c]))
function align(id: string): string {
  const a = colMeta.get(id as ColId)?.align
  return a === 'left' ? 'left' : a === 'center' ? 'center' : ''
}

// ---- мобильная сортировка ----
const mobileSortId = computed({
  get: () => sorting.value[0]?.id ?? 'status',
  set: (id: string) => {
    sorting.value = [{ id, desc: sorting.value[0]?.desc ?? false }]
  },
})
function toggleMobileDir(): void {
  const s = sorting.value[0] ?? { id: 'status', desc: false }
  sorting.value = [{ id: s.id, desc: !s.desc }]
}

const totalRows = computed(() => screener.state.rows.length)
const loading = computed(() => !screener.state.ready)
</script>

<template>
  <div class="space-y-3">
    <div class="flex flex-wrap items-baseline justify-between gap-2">
      <h1 class="text-lg font-semibold">Скринер</h1>
      <p class="text-muted-sm num" data-testid="screener-meta">
        <span data-testid="row-count">{{ sortedRows.length }}</span> из {{ totalRows }} · ТФ {{ screener.tf }} · обновлено
        {{ fmtDateTime(screener.state.updatedAt, prefs.tz, { seconds: true, year: false }) }}
      </p>
    </div>

    <ScreenerFilters :compact="!isDesktop" />

    <p
      v-if="screener.calibrationStale"
      class="rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn"
      role="alert"
      data-testid="stale-banner"
    >
      Калибровка устарела: фактическая частота live-сигналов расходится с предсказанной. Вероятности показаны серым до переобучения.
    </p>
    <p v-if="screener.error" class="rounded-md border border-short/40 bg-short/10 px-3 py-2 text-sm text-short" role="alert">
      {{ screener.error }}
    </p>

    <!-- Десктоп: таблица -->
    <div
      v-if="isDesktop"
      class="card max-h-[calc(100vh-15rem)] overflow-auto"
      :class="loading ? 'opacity-60' : ''"
      data-testid="screener-table"
      :aria-busy="loading"
    >
      <table class="tbl text-[13px]">
        <thead>
          <tr v-for="hg in table.getHeaderGroups()" :key="hg.id">
            <th
              v-for="h in hg.headers"
              :key="h.id"
              :class="[align(h.column.id), h.column.id === 'symbol' ? 'sticky left-0 z-[3]' : '']"
              :aria-sort="h.column.getIsSorted() === 'asc' ? 'ascending' : h.column.getIsSorted() === 'desc' ? 'descending' : 'none'"
              scope="col"
            >
              <button
                type="button"
                class="inline-flex items-center gap-0.5 hover:text-fg"
                :title="`${colMeta.get(h.column.id as ColId)?.title ?? ''}. Клик — сортировка, Shift+клик — по нескольким колонкам`"
                :data-testid="`sort-${h.column.id}`"
                @click="h.column.getToggleSortingHandler()?.($event)"
              >
                {{ h.column.columnDef.header }}
                <span class="w-2.5 text-accent" aria-hidden="true">{{
                  h.column.getIsSorted() === 'asc' ? '↑' : h.column.getIsSorted() === 'desc' ? '↓' : ''
                }}</span>
              </button>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="row in table.getRowModel().rows" :key="row.id" :data-symbol="row.original.hh_symbol" data-testid="screener-row">
            <td
              v-for="cell in row.getVisibleCells()"
              :key="cell.id"
              :class="[align(cell.column.id), cell.column.id === 'symbol' ? 'sticky left-0 z-[1] bg-surface' : '']"
              :data-col="cell.column.id"
            >
              <ScreenerCell
                :row="row.original"
                :col="cell.column.id as ColId"
                :stale="screener.calibrationStale"
              />
            </td>
          </tr>
          <tr v-if="!sortedRows.length">
            <td :colspan="table.getVisibleLeafColumns().length" class="center py-8 text-muted">
              {{ loading ? 'Загрузка…' : 'Нет инструментов под выбранные фильтры' }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <!-- Телефон: карточки -->
    <div v-else class="space-y-2" data-testid="screener-cards" :aria-busy="loading">
      <div class="flex items-center gap-2">
        <label class="text-muted-sm" for="m-sort">Сортировка</label>
        <select id="m-sort" v-model="mobileSortId" class="input min-w-0 flex-1 py-1 text-sm">
          <option v-for="c in SCREENER_COLUMNS" :key="c.id" :value="c.id">{{ c.header }} — {{ c.title }}</option>
        </select>
        <button type="button" class="btn btn-sm" :aria-label="sorting[0]?.desc ? 'По убыванию' : 'По возрастанию'" @click="toggleMobileDir">
          {{ sorting[0]?.desc ? '↓' : '↑' }}
        </button>
      </div>

      <article
        v-for="r in sortedRows"
        :key="r.hh_symbol"
        class="card p-3"
        data-testid="screener-card"
        :data-symbol="r.hh_symbol"
      >
        <header class="flex items-start justify-between gap-2">
          <div class="min-w-0">
            <RouterLink :to="`/instrument/${r.hh_symbol}`" class="text-base font-semibold hover:text-accent">{{ r.base }}</RouterLink>
            <span class="ml-1 text-muted-sm">{{ ASSET_CLASS_RU[r.asset_class] }}</span>
            <div class="num">
              <span data-col="price">{{ fmtPrice(r.price) }}</span>
              <span class="ml-2" :class="signClass(r.change_24h_pct)" data-col="change">{{ fmtPct(r.change_24h_pct, 2, true) }}</span>
            </div>
          </div>
          <StatusBadge :status="r.status" :side="r.active_signal_side" />
        </header>

        <div class="mt-2 grid grid-cols-2 gap-2">
          <div v-if="screener.filters.side !== 'short'" class="rounded-md bg-surface-2 p-2">
            <div class="flex items-center justify-between text-muted-sm">
              <span>Лонг · score <b class="num text-fg" data-col="score_long">{{ fmtNum(r.score_long, 0) }}</b></span>
            </div>
            <div class="mt-1 flex items-center justify-between gap-1">
              <ProbCell :prob="r.p_long" side="long" :status="r.status" :stale="screener.calibrationStale" :score="r.score_long" detailed data-col="p_long" />
              <span class="num text-sm" :class="signClass(r.ev_long_r)" data-col="ev_long">{{ fmtR(r.ev_long_r) }}</span>
            </div>
          </div>
          <div v-if="screener.filters.side !== 'long'" class="rounded-md bg-surface-2 p-2">
            <div class="flex items-center justify-between text-muted-sm">
              <span>Шорт · score <b class="num text-fg" data-col="score_short">{{ fmtNum(r.score_short, 0) }}</b></span>
            </div>
            <div class="mt-1 flex items-center justify-between gap-1">
              <ProbCell :prob="r.p_short" side="short" :status="r.status" :stale="screener.calibrationStale" :score="r.score_short" detailed data-col="p_short" />
              <span class="num text-sm" :class="signClass(r.ev_short_r)" data-col="ev_short">{{ fmtR(r.ev_short_r) }}</span>
            </div>
          </div>
        </div>

        <dl class="mt-2 grid grid-cols-3 gap-x-2 gap-y-1 text-xs sm:grid-cols-6">
          <div><dt class="text-muted">HTF</dt><dd><TrendIcon :value="r.htf_trend" label="HTF-тренд" /></dd></div>
          <div><dt class="text-muted">RSI</dt><dd class="num" :class="rsiClass(r.rsi14)" data-col="rsi">{{ fmtNum(r.rsi14, 1) }}</dd></div>
          <div><dt class="text-muted">ADX</dt><dd class="num" data-col="adx">{{ fmtNum(r.adx, 1) }}</dd></div>
          <div><dt class="text-muted">VWAP</dt><dd class="num" :class="signClass(r.vwap_dist_pct)" data-col="vwap">{{ fmtPct(r.vwap_dist_pct, 2, true) }}</dd></div>
          <div><dt class="text-muted">ATR</dt><dd class="num" data-col="atr">{{ fmtPct(r.atr_pct, 2) }}</dd></div>
          <div><dt class="text-muted">Funding</dt><dd class="num" data-col="funding">{{ fmtFunding(r.funding_rate) }}</dd></div>
          <div><dt class="text-muted">ΔOI 24h</dt><dd class="num" :class="signClass(r.oi_change_24h_pct)" data-col="oi">{{ fmtPct(r.oi_change_24h_pct, 1, true) }}</dd></div>
          <div><dt class="text-muted">CVD</dt><dd><TrendIcon :value="r.cvd_trend" label="CVD-тренд" /></dd></div>
          <div><dt class="text-muted">Лимит</dt><dd class="num" data-col="limit">{{ fmtUsdCompact(r.max_per_lot_usd) }}</dd></div>
        </dl>
      </article>
      <p v-if="!sortedRows.length" class="py-8 text-center text-muted">
        {{ loading ? 'Загрузка…' : 'Нет инструментов под выбранные фильтры' }}
      </p>
    </div>
  </div>
</template>
