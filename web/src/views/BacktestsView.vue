<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef, watch } from 'vue'
import { api } from '@/api/endpoints'
import { errorMessage } from '@/api/client'
import type { BacktestKind, BacktestRun, Trial } from '@/api/types'
import { usePrefsStore } from '@/stores/prefs'
import { fmtDateTime } from '@/utils/format'
import { BACKTEST_KIND_RU, GROUP_RU } from '@/utils/labels'
import { METRICS, bestRunIds } from '@/utils/backtests'

const prefs = usePrefsStore()

const kind = ref<BacktestKind | ''>('')
const runs = shallowRef<BacktestRun[]>([])
const loading = ref(false)
const error = ref<string | null>(null)
const selected = ref<number[]>([])

async function loadRuns(): Promise<void> {
  loading.value = true
  try {
    runs.value = await api.backtests(kind.value, 50)
    error.value = null
  } catch (e) {
    error.value = errorMessage(e)
  } finally {
    loading.value = false
  }
}
watch(kind, loadRuns, { immediate: true })

const compared = computed(() => runs.value.filter((r) => selected.value.includes(r.id)))
const shortMetrics = METRICS.filter((m) => ['p_pass_s1', 'p_pass_s2', 'p_pass_both', 'p_fail_daily', 'p_unresolved', 'ev_usd', 'dsr', 'pbo'].includes(m.key))

function toggleSelect(id: number): void {
  selected.value = selected.value.includes(id) ? selected.value.filter((x) => x !== id) : [...selected.value, id]
}

// ---- отчёт ----
const report = shallowRef<{ run: BacktestRun; html: string; blobUrl: string } | null>(null)
const reportLoading = ref<number | null>(null)
const reportError = ref<string | null>(null)

function closeReport(): void {
  if (report.value) URL.revokeObjectURL(report.value.blobUrl)
  report.value = null
}

async function openReport(run: BacktestRun): Promise<void> {
  if (!run.report_url) return
  reportLoading.value = run.id
  reportError.value = null
  try {
    const html = await api.backtestReport(run.report_url)
    closeReport()
    const blobUrl = URL.createObjectURL(new Blob([html], { type: 'text/html' }))
    report.value = { run, html, blobUrl }
  } catch (e) {
    reportError.value = errorMessage(e)
  } finally {
    reportLoading.value = null
  }
}
onBeforeUnmount(closeReport)

// ---- реестр испытаний ----
const trials = shallowRef<Trial[]>([])
const trialsTotal = ref(0)
const study = ref('')
const trialsLimit = ref(100)
const trialsError = ref<string | null>(null)
const studies = ref<string[]>([])

async function loadTrials(): Promise<void> {
  try {
    const res = await api.trials(study.value || undefined, trialsLimit.value)
    trials.value = res.items
    trialsTotal.value = res.total
    if (!study.value) studies.value = [...new Set(res.items.map((t) => t.study))].sort()
    trialsError.value = null
  } catch (e) {
    trialsError.value = errorMessage(e)
  }
}
watch([study, trialsLimit], loadTrials, { immediate: true })

function fmtKv(obj: Record<string, unknown>): { k: string; v: string }[] {
  return Object.entries(obj).map(([k, v]) => ({
    k,
    v: typeof v === 'number' ? (Number.isInteger(v) ? String(v) : v.toFixed(Math.abs(v) < 1 ? 3 : 2)) : typeof v === 'string' ? v : JSON.stringify(v),
  }))
}

function groupLabel(g: string): string {
  return (GROUP_RU as Record<string, string>)[g] ?? g
}
</script>

<template>
  <div class="space-y-3">
    <div class="flex flex-wrap items-end justify-between gap-2">
      <h1 class="text-lg font-semibold">Бэктесты</h1>
      <div class="flex items-end gap-2">
        <div>
          <label class="label" for="bt-kind">Тип прогона</label>
          <select id="bt-kind" v-model="kind" class="input py-1 text-sm">
            <option value="">все</option>
            <option v-for="(label, k) in BACKTEST_KIND_RU" :key="k" :value="k">{{ label }}</option>
          </select>
        </div>
        <button type="button" class="btn btn-sm" :disabled="loading" @click="loadRuns">Обновить</button>
      </div>
    </div>

    <p v-if="error" class="text-short" role="alert">{{ error }}</p>

    <section class="card overflow-x-auto" :aria-busy="loading">
      <table class="tbl text-[13px]" data-testid="runs-table">
        <thead>
          <tr>
            <th class="center" title="Выбрать для сравнения">Срав.</th>
            <th class="left">#</th>
            <th class="left">Дата</th>
            <th class="left">Тип</th>
            <th class="left">Стратегия</th>
            <th v-for="m in shortMetrics" :key="m.key" :title="m.title">{{ m.label }}</th>
            <th class="left">Версии</th>
            <th />
          </tr>
        </thead>
        <tbody>
          <tr v-for="r in runs" :key="r.id" data-testid="run-row" :data-run-id="r.id">
            <td class="center">
              <input
                type="checkbox"
                class="h-4 w-4"
                :checked="selected.includes(r.id)"
                :aria-label="`Сравнить прогон ${r.id}`"
                data-testid="run-compare"
                @change="toggleSelect(r.id)"
              />
            </td>
            <td class="left num">{{ r.id }}</td>
            <td class="left num">{{ fmtDateTime(r.created_at, prefs.tz, { year: false }) }}</td>
            <td class="left">{{ BACKTEST_KIND_RU[r.kind] }}</td>
            <td class="left font-semibold">{{ r.strategy }}</td>
            <td v-for="m in shortMetrics" :key="m.key" class="num" :data-metric="m.key">{{ m.fmt(r.metrics[m.key]) }}</td>
            <td class="left num text-xs text-muted" :title="`params ${r.params_hash} · dataset ${r.dataset_hash}`">
              {{ r.code_version }}<br />d:{{ r.dataset_hash.slice(0, 8) }} p:{{ r.params_hash.slice(0, 8) }}
            </td>
            <td>
              <button
                type="button"
                class="btn btn-sm"
                :disabled="!r.report_url || reportLoading === r.id"
                data-testid="open-report"
                @click="openReport(r)"
              >
                {{ reportLoading === r.id ? '…' : 'Отчёт' }}
              </button>
            </td>
          </tr>
          <tr v-if="!runs.length">
            <td :colspan="7 + shortMetrics.length" class="center py-6 text-muted">{{ loading ? 'Загрузка…' : 'Прогонов нет' }}</td>
          </tr>
        </tbody>
      </table>
    </section>
    <p v-if="reportError" class="text-short" role="alert">{{ reportError }}</p>

    <!-- Отчёт -->
    <section v-if="report" class="card space-y-2 p-3" data-testid="report-viewer">
      <div class="flex flex-wrap items-center gap-2">
        <h2 class="font-semibold">Отчёт прогона #{{ report.run.id }} · {{ report.run.strategy }}</h2>
        <a :href="report.blobUrl" target="_blank" rel="noopener" class="btn btn-sm ml-auto">Открыть в новой вкладке ↗</a>
        <button type="button" class="btn btn-sm" @click="closeReport">Закрыть</button>
      </div>
      <iframe
        :srcdoc="report.html"
        sandbox="allow-scripts allow-popups"
        class="h-[70vh] w-full rounded-md border border-line bg-white"
        :title="`Отчёт прогона ${report.run.id}`"
        data-testid="report-iframe"
      />
    </section>

    <!-- Сравнение -->
    <section class="card space-y-2 p-3" data-testid="compare-panel">
      <div class="flex flex-wrap items-center gap-2">
        <h2 class="font-semibold">Сравнение прогонов</h2>
        <span class="text-muted-sm">отметьте 2 и более прогона в таблице</span>
        <button v-if="selected.length" type="button" class="btn btn-sm ml-auto" @click="selected = []">Очистить</button>
      </div>
      <div v-if="compared.length >= 2" class="overflow-x-auto">
        <table class="tbl text-[13px]" data-testid="compare-table">
          <thead>
            <tr>
              <th class="left">Метрика</th>
              <th v-for="r in compared" :key="r.id">#{{ r.id }} {{ r.strategy }}<br /><span class="font-normal">{{ BACKTEST_KIND_RU[r.kind] }}</span></th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="m in METRICS" :key="m.key">
              <td class="left" :title="m.title">{{ m.label }}</td>
              <td
                v-for="r in compared"
                :key="r.id"
                class="num"
                :class="bestRunIds(compared, m).has(r.id) ? 'font-semibold text-long' : ''"
                :data-best="bestRunIds(compared, m).has(r.id) ? '1' : undefined"
              >
                {{ m.fmt(r.metrics[m.key]) }}
              </td>
            </tr>
            <tr>
              <td class="left">Датасет / параметры</td>
              <td v-for="r in compared" :key="r.id" class="num text-xs">{{ r.dataset_hash.slice(0, 8) }} / {{ r.params_hash.slice(0, 8) }}</td>
            </tr>
          </tbody>
        </table>
        <p class="mt-1 text-muted-sm">Зелёным — лучшее значение метрики. Сравнивать корректно только прогоны на одном датасете.</p>
      </div>
      <p v-else class="text-muted">Выбрано: {{ selected.length }}</p>
    </section>

    <!-- Реестр испытаний -->
    <section class="card space-y-2 p-3" data-testid="trials-panel">
      <div class="flex flex-wrap items-end gap-2">
        <h2 class="mr-auto font-semibold">Реестр испытаний <span class="num text-muted-sm">({{ trialsTotal }})</span></h2>
        <div>
          <label class="label" for="tr-study">Исследование</label>
          <select id="tr-study" v-model="study" class="input py-1 text-sm">
            <option value="">все</option>
            <option v-for="s in studies" :key="s" :value="s">{{ s }}</option>
          </select>
        </div>
        <div>
          <label class="label" for="tr-limit">Показать</label>
          <select id="tr-limit" v-model.number="trialsLimit" class="input py-1 text-sm">
            <option :value="50">50</option>
            <option :value="100">100</option>
            <option :value="500">500</option>
          </select>
        </div>
      </div>
      <p v-if="trialsError" class="text-short" role="alert">{{ trialsError }}</p>
      <div class="max-h-[60vh] overflow-auto">
        <table class="tbl text-[13px]" data-testid="trials-table">
          <thead>
            <tr>
              <th class="left">#</th>
              <th class="left">Дата</th>
              <th class="left">Исследование</th>
              <th>Уровень</th>
              <th class="left">Стратегия</th>
              <th class="left">Группа</th>
              <th class="left">Параметры</th>
              <th class="left">Метрики</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="t in trials" :key="t.id" data-testid="trial-row">
              <td class="left num">{{ t.id }}</td>
              <td class="left num">{{ fmtDateTime(t.created_at, prefs.tz, { year: false }) }}</td>
              <td class="left">{{ t.study }}</td>
              <td class="num">{{ t.level }}</td>
              <td class="left">{{ t.strategy }}</td>
              <td class="left">{{ groupLabel(t.group) }}</td>
              <td class="left whitespace-normal">
                <span v-for="kv in fmtKv(t.params)" :key="kv.k" class="mr-1 mb-0.5 inline-block rounded bg-surface-2 px-1 num text-xs">
                  {{ kv.k }}={{ kv.v }}
                </span>
              </td>
              <td class="left whitespace-normal">
                <span v-for="kv in fmtKv(t.metrics)" :key="kv.k" class="mr-1 mb-0.5 inline-block rounded bg-surface-2 px-1 num text-xs">
                  {{ kv.k }}={{ kv.v }}
                </span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <p class="text-muted-sm">Каждая проверенная конфигурация учитывается в DSR/PBO — поправка на множественное тестирование.</p>
    </section>
  </div>
</template>
