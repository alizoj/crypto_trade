<script setup lang="ts">
import { computed, onBeforeUnmount, reactive, ref, shallowRef, watch } from 'vue'
import { api } from '@/api/endpoints'
import { errorMessage } from '@/api/client'
import type { AnalysisResponse, BarsResponse, Side, Timeframe } from '@/api/types'
import PriceChart, { type Overlays } from '@/components/instrument/PriceChart.vue'
import TradePlanCard from '@/components/instrument/TradePlanCard.vue'
import SetupStatsCard from '@/components/instrument/SetupStatsCard.vue'
import FactorsTable from '@/components/instrument/FactorsTable.vue'
import ProbCell from '@/components/ProbCell.vue'
import Sparkline from '@/components/Sparkline.vue'
import TrendIcon from '@/components/TrendIcon.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import { useScreenerStore } from '@/stores/screener'
import { usePrefsStore, effectiveTheme } from '@/stores/prefs'
import { useAccountsStore } from '@/stores/accounts'
import { useMetaStore } from '@/stores/meta'
import { useInstrumentsStore } from '@/stores/instruments'
import { fmtDateTime, fmtFunding, fmtNum, fmtPct, fmtPrice, fmtR, fmtUsd, fmtUsdCompact } from '@/utils/format'
import { signClass, trendLabel } from '@/utils/colors'
import { ASSET_CLASS_RU, GROUP_RU, SIDE_RU } from '@/utils/labels'
import { STORAGE_KEYS, readJSON, writeJSON } from '@/utils/storage'

const props = defineProps<{ symbol: string }>()

const screener = useScreenerStore()
const prefs = usePrefsStore()
const accounts = useAccountsStore()
const meta = useMetaStore()
const instruments = useInstrumentsStore()
void instruments.load()

const TF_SEC: Record<Timeframe, number> = { '15m': 900, '1h': 3600, '4h': 14400 }
const tf = ref<Timeframe>(screener.tf)
const sym = computed(() => props.symbol.toUpperCase())

const bars = shallowRef<BarsResponse | null>(null)
const analysis = shallowRef<AnalysisResponse | null>(null)
const barsError = ref<string | null>(null)
const analysisError = ref<string | null>(null)
const loadingBars = ref(false)
const loadingAnalysis = ref(false)
const chartRef = ref<InstanceType<typeof PriceChart> | null>(null)

const DEFAULT_OVERLAYS: Overlays = { ema: true, vwap: true, avwap: true, levels: true, markers: true }
const overlays = reactive<Overlays>({ ...DEFAULT_OVERLAYS, ...readJSON<Partial<Overlays>>(STORAGE_KEYS.instrumentOverlays, {}) })
watch(overlays, (o) => writeJSON(STORAGE_KEYS.instrumentOverlays, o), { deep: true })
const overlayItems: { key: keyof Overlays; label: string; colors: string[] }[] = [
  { key: 'ema', label: 'EMA 20/50/200', colors: ['var(--ema20)', 'var(--ema50)', 'var(--ema200)'] },
  { key: 'vwap', label: 'VWAP', colors: ['var(--vwap)'] },
  { key: 'avwap', label: 'AVWAP', colors: ['var(--avwap)'] },
  { key: 'levels', label: 'POC/VAH/VAL', colors: ['var(--level)'] },
  { key: 'markers', label: 'Сигналы', colors: ['var(--long)', 'var(--short)'] },
]

const themeKey = computed(() => `${effectiveTheme(prefs.theme)}|${prefs.systemThemeTick}`)
const accountId = computed(() => accounts.selected?.id ?? null)
const instrument = computed(() => instruments.bySymbol.get(sym.value) ?? null)
const liveRow = computed(() => screener.findRow(sym.value))

let barsCtl: AbortController | null = null
let analysisCtl: AbortController | null = null

async function loadBars(): Promise<void> {
  barsCtl?.abort()
  const ctl = new AbortController()
  barsCtl = ctl
  loadingBars.value = true
  try {
    bars.value = await api.bars(sym.value, tf.value, 500, undefined, ctl.signal)
    barsError.value = null
  } catch (e) {
    if (!ctl.signal.aborted) barsError.value = errorMessage(e)
  } finally {
    if (barsCtl === ctl) loadingBars.value = false
  }
}

async function loadAnalysis(): Promise<void> {
  analysisCtl?.abort()
  const ctl = new AbortController()
  analysisCtl = ctl
  loadingAnalysis.value = true
  try {
    analysis.value = await api.analysis(sym.value, tf.value, accountId.value, ctl.signal)
    analysisError.value = null
  } catch (e) {
    if (!ctl.signal.aborted) analysisError.value = errorMessage(e)
  } finally {
    if (analysisCtl === ctl) loadingAnalysis.value = false
  }
}

watch([sym, tf], () => {
  bars.value = null
  analysis.value = null
  void loadBars()
  void loadAnalysis()
}, { immediate: true })
watch(accountId, () => void loadAnalysis())

// Live: последняя свеча — по цене из WS скринера; на закрытии бара — перезагрузка.
watch(
  () => liveRow.value?.price,
  (p) => {
    if (typeof p === 'number') chartRef.value?.updateLastPrice(p)
  },
)
const barIdx = () => Math.floor(meta.serverNowMs / 1000 / TF_SEC[tf.value])
let lastBarIdx = barIdx()
watch(tf, () => (lastBarIdx = barIdx()))
const barTimer = setInterval(() => {
  const idx = barIdx()
  if (idx !== lastBarIdx) {
    lastBarIdx = idx
    setTimeout(() => {
      void loadBars()
      void loadAnalysis()
    }, 3000)
  }
}, 5000)
onBeforeUnmount(() => {
  clearInterval(barTimer)
  barsCtl?.abort()
  analysisCtl?.abort()
})

const price = computed(() => liveRow.value?.price ?? bars.value?.bars.at(-1)?.c ?? null)
const activeSignal = computed(() => bars.value?.active_signal ?? null)
// статус строки скринера относится к ТФ скринера; для другого ТФ — по активному сигналу этого ТФ
const status = computed(() => {
  const row = liveRow.value
  if (!row) return null
  if (tf.value === screener.tf || row.status === 'off_session' || row.status === 'halted') return row.status
  return activeSignal.value ? 'signal' : null
})
const statusSide = computed(() =>
  tf.value === screener.tf ? (liveRow.value?.active_signal_side ?? null) : (activeSignal.value?.side ?? null),
)
const sides: Side[] = ['long', 'short']

const fundingLast = computed(() => analysis.value?.context.funding.at(-1)?.v ?? null)
const oiChange = computed(() => {
  const oi = analysis.value?.context.oi
  if (!oi || oi.length < 2) return null
  const a = oi[0]!.v
  const b = oi[oi.length - 1]!.v
  return a ? ((b - a) / a) * 100 : null
})
</script>

<template>
  <div class="space-y-3" data-testid="instrument-view">
    <!-- Заголовок -->
    <section class="flex flex-wrap items-center gap-x-4 gap-y-2">
      <RouterLink to="/" class="text-sm text-muted hover:text-fg">← Скринер</RouterLink>
      <h1 class="text-xl font-semibold" data-testid="instrument-symbol">{{ sym }}</h1>
      <span v-if="instrument" class="text-muted-sm">
        {{ ASSET_CLASS_RU[instrument.asset_class] }} · {{ GROUP_RU[instrument.group] }} · {{ instrument.underlying_type }}
      </span>
      <span class="num text-lg font-semibold" data-testid="instrument-price">{{ fmtPrice(price) }}</span>
      <span v-if="liveRow" class="num" :class="signClass(liveRow.change_24h_pct)">{{ fmtPct(liveRow.change_24h_pct, 2, true) }}</span>
      <StatusBadge v-if="status" :status="status" :side="statusSide" />
      <div class="seg ml-auto" role="group" aria-label="Таймфрейм">
        <button
          v-for="t in meta.timeframes"
          :key="t"
          type="button"
          :aria-pressed="tf === t"
          :data-testid="`inst-tf-${t}`"
          @click="tf = t"
        >
          {{ t }}
        </button>
      </div>
    </section>
    <p v-if="instrument" class="text-muted-sm num">
      Лимит HashHedge: {{ fmtUsd(instrument.max_per_lot_usd) }} на позицию · {{ fmtUsd(instrument.max_total_usd) }} всего · Binance
      {{ instrument.binance_um }}<template v-if="instrument.bybit_linear"> · Bybit {{ instrument.bybit_linear }}</template
      ><template v-if="instrument.notes"> · {{ instrument.notes }}</template>
    </p>

    <div class="grid grid-cols-1 gap-3 xl:grid-cols-[minmax(0,1fr)_420px]">
      <!-- График -->
      <section class="card min-w-0 p-2">
        <div class="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 px-1 text-xs">
          <label v-for="o in overlayItems" :key="o.key" class="flex cursor-pointer items-center gap-1 select-none">
            <input v-model="overlays[o.key]" type="checkbox" class="h-3.5 w-3.5" />
            <span class="inline-flex gap-px" aria-hidden="true"
              ><span v-for="c in o.colors" :key="c" class="inline-block h-0.5 w-2.5 rounded" :style="{ background: c }"
            /></span>
            {{ o.label }}
          </label>
          <button type="button" class="btn btn-sm ml-auto" @click="chartRef?.resetView()">К последним барам</button>
        </div>
        <p v-if="barsError" class="p-4 text-short" role="alert">{{ barsError }}</p>
        <div v-else-if="!bars" class="flex h-[460px] items-center justify-center text-muted sm:h-[600px]">
          {{ loadingBars ? 'Загрузка графика…' : 'Нет данных' }}
        </div>
        <PriceChart v-else ref="chartRef" :data="bars" :tz="prefs.tz" :theme-key="themeKey" :overlays="overlays" />
        <p v-if="bars" class="mt-1 px-1 text-muted-sm">
          Панели: цена · RSI(14) · объём (гистограмма) и CVD (линия). Прокрутка и масштаб синхронизированы.
          <template v-if="bars.volume_profile">
            POC {{ fmtPrice(bars.volume_profile.poc) }}, VAH {{ fmtPrice(bars.volume_profile.vah) }}, VAL
            {{ fmtPrice(bars.volume_profile.val) }}.
          </template>
          <template v-if="activeSignal">
            Активный сигнал #{{ activeSignal.id }} ({{ SIDE_RU[activeSignal.side] }}): линии входа, SL и TP на графике.
          </template>
        </p>
      </section>

      <!-- Вероятности и контекст -->
      <aside class="min-w-0 space-y-3">
        <section class="card p-3" data-testid="prob-panel">
          <h2 class="mb-2 font-semibold">Вероятности · {{ tf }}</h2>
          <p v-if="analysisError" class="text-short" role="alert">{{ analysisError }}</p>
          <div v-else-if="analysis" class="grid grid-cols-2 gap-2">
            <div v-for="s in sides" :key="s" class="rounded-md bg-surface-2 p-2" :data-testid="`prob-${s}`">
              <div class="text-muted-sm">{{ SIDE_RU[s] }} · score <b class="num text-fg">{{ fmtNum(analysis[s].score, 0) }}</b></div>
              <div class="mt-1 text-lg">
                <ProbCell :prob="analysis[s].prob" :side="s" :status="liveRow?.status" :score="analysis[s].score" :stale="screener.calibrationStale" detailed />
              </div>
              <div class="mt-1 text-sm">
                EV <b class="num" :class="signClass(analysis[s].ev_r)" data-field="ev">{{ fmtR(analysis[s].ev_r) }}</b>
                <span v-if="analysis[s].prob" class="text-muted-sm"> · n={{ analysis[s].prob!.n }}</span>
              </div>
            </div>
          </div>
          <p v-else class="text-muted">Загрузка…</p>
          <p class="mt-2 text-muted-sm">
            P — откалиброванная частота «TP раньше SL» для похожих сетапов (Wilson 95%). P(long) и P(short) не дополняют друг друга до
            100%.
          </p>
        </section>

        <section v-if="analysis" class="card space-y-2 p-3 text-[13px]" data-testid="context-panel">
          <h2 class="font-semibold">Контекст</h2>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <div class="text-muted-sm">Funding · сейчас <b class="num text-fg">{{ fmtFunding(fundingLast) }}</b></div>
              <Sparkline :points="analysis.context.funding" zero-line label="История funding" :width="180" :height="42" />
            </div>
            <div>
              <div class="text-muted-sm">
                OI · <b class="num" :class="signClass(oiChange)">{{ fmtPct(oiChange, 1, true) }}</b>
                <span v-if="analysis.context.oi.length" class="num"> ({{ fmtUsdCompact(analysis.context.oi.at(-1)!.v) }})</span>
              </div>
              <Sparkline :points="analysis.context.oi" label="Открытый интерес" color="var(--ema200)" :width="180" :height="42" />
            </div>
          </div>
          <div class="flex flex-wrap gap-x-4 gap-y-1">
            <span v-if="analysis.context.btc_trend !== null">
              Тренд BTC: <TrendIcon :value="analysis.context.btc_trend" label="BTC" /> {{ trendLabel(analysis.context.btc_trend) }}
            </span>
            <span v-if="analysis.context.session">
              Сессия базового рынка:
              <b :class="analysis.context.session.open ? 'text-long' : 'text-muted'">{{ analysis.context.session.open ? 'открыта' : 'закрыта' }}</b>
              <template v-if="analysis.context.session.next_change">
                · {{ analysis.context.session.open ? 'закроется' : 'откроется' }}
                {{ fmtDateTime(analysis.context.session.next_change, prefs.tz, { year: false }) }}
              </template>
            </span>
          </div>
        </section>
      </aside>
    </div>

    <!-- Анализ -->
    <div v-if="analysis" class="grid grid-cols-1 gap-3 lg:grid-cols-2">
      <section class="card space-y-3 p-3" data-testid="analysis-panel">
        <div class="flex items-baseline justify-between gap-2">
          <h2 class="font-semibold">Разбор сетапа</h2>
          <span class="text-muted-sm num">{{ fmtDateTime(analysis.ts, prefs.tz, { year: false }) }}</span>
        </div>
        <ul class="list-disc space-y-1 pl-5 text-[13px]" data-testid="analysis-text">
          <li v-for="(t, i) in analysis.text" :key="i">{{ t }}</li>
        </ul>
        <div v-if="analysis.llm_summary" class="rounded-md border border-dashed border-line p-2 text-[13px]" data-testid="llm-summary">
          <div class="mb-1 flex items-center gap-2">
            <span class="badge badge-accent">LLM-резюме</span>
            <span class="text-muted-sm">сгенерировано по тем же рассчитанным фактам, без новых утверждений</span>
          </div>
          {{ analysis.llm_summary }}
        </div>
        <FactorsTable :long="analysis.long.factors" :short="analysis.short.factors" />
      </section>

      <section class="card space-y-3 p-3">
        <div class="flex flex-wrap items-baseline justify-between gap-2">
          <h2 class="font-semibold">План сделки</h2>
          <span class="text-muted-sm">
            <template v-if="accounts.selected">
              счёт «{{ accounts.selected.name }}» · дневной запас {{ fmtUsd(accounts.selected.daily_room_usd) }}
            </template>
            <template v-else>счёт не выбран</template>
          </span>
        </div>
        <div class="grid grid-cols-1 gap-3 md:grid-cols-2">
          <div v-for="s in sides" :key="s" class="space-y-2">
            <h3 class="text-sm font-semibold" :class="s === 'long' ? 'text-long' : 'text-short'">{{ SIDE_RU[s] }}</h3>
            <TradePlanCard :plan="analysis[s].plan" :account="accounts.selected" :tz="prefs.tz" />
            <h4 class="pt-1 text-xs font-semibold text-muted uppercase">Статистика сетапа (бэктест)</h4>
            <SetupStatsCard :stats="analysis[s].setup_stats" />
          </div>
        </div>
      </section>
    </div>
    <p v-else-if="loadingAnalysis" class="text-muted">Загрузка анализа…</p>
  </div>
</template>
