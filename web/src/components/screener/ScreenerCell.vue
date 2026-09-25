<script setup lang="ts">
import type { ScreenerRow } from '@/api/types'
import type { ColId } from './columns'
import ProbCell from '@/components/ProbCell.vue'
import StatusBadge from '@/components/StatusBadge.vue'
import TrendIcon from '@/components/TrendIcon.vue'
import { fmtFunding, fmtNum, fmtPct, fmtPrice, fmtR, fmtUsdCompact, fmtUsd, fmtAge } from '@/utils/format'
import { rsiClass, signClass } from '@/utils/colors'
import { ASSET_CLASS_RU, GROUP_RU } from '@/utils/labels'

defineProps<{ row: ScreenerRow; col: ColId; stale: boolean }>()
</script>

<template>
  <template v-if="col === 'symbol'">
    <RouterLink
      :to="`/instrument/${row.hh_symbol}`"
      class="font-semibold hover:text-accent hover:underline"
      :title="`${row.hh_symbol} · ${GROUP_RU[row.group]} · данные ${fmtAge(row.data_age_sec)} назад`"
      >{{ row.base }}</RouterLink
    >
    <span v-if="(row.data_age_sec ?? 0) > 300" class="ml-1 text-warn" :title="`Нет новых баров ${fmtAge(row.data_age_sec)}`">⚠</span>
  </template>
  <span v-else-if="col === 'class'" class="text-muted">{{ ASSET_CLASS_RU[row.asset_class] }}</span>
  <span v-else-if="col === 'price'" class="num">{{ fmtPrice(row.price) }}</span>
  <span v-else-if="col === 'change'" class="num" :class="signClass(row.change_24h_pct)">{{ fmtPct(row.change_24h_pct, 2, true) }}</span>
  <TrendIcon v-else-if="col === 'htf'" :value="row.htf_trend" label="HTF-тренд" />
  <span v-else-if="col === 'rsi'" class="num" :class="rsiClass(row.rsi14)">{{ fmtNum(row.rsi14, 1) }}</span>
  <span v-else-if="col === 'adx'" class="num" :class="(row.adx ?? 0) < 20 ? 'text-muted' : ''">{{ fmtNum(row.adx, 1) }}</span>
  <span v-else-if="col === 'vwap'" class="num" :class="signClass(row.vwap_dist_pct)">{{ fmtPct(row.vwap_dist_pct, 2, true) }}</span>
  <span v-else-if="col === 'atr'" class="num">{{ fmtPct(row.atr_pct, 2) }}</span>
  <span
    v-else-if="col === 'funding'"
    class="num"
    :class="row.funding_rate !== null && Math.abs(row.funding_rate) >= 0.0003 ? 'text-warn font-semibold' : ''"
    >{{ fmtFunding(row.funding_rate) }}</span
  >
  <span v-else-if="col === 'oi'" class="num" :class="signClass(row.oi_change_24h_pct)">{{ fmtPct(row.oi_change_24h_pct, 1, true) }}</span>
  <TrendIcon v-else-if="col === 'cvd'" :value="row.cvd_trend" label="CVD-тренд" />
  <span v-else-if="col === 'score_long'" class="num">{{ fmtNum(row.score_long, 0) }}</span>
  <span v-else-if="col === 'score_short'" class="num">{{ fmtNum(row.score_short, 0) }}</span>
  <ProbCell v-else-if="col === 'p_long'" :prob="row.p_long" side="long" :status="row.status" :stale="stale" :score="row.score_long" />
  <ProbCell v-else-if="col === 'p_short'" :prob="row.p_short" side="short" :status="row.status" :stale="stale" :score="row.score_short" />
  <span v-else-if="col === 'ev_long'" class="num" :class="signClass(row.ev_long_r)">{{ fmtR(row.ev_long_r) }}</span>
  <span v-else-if="col === 'ev_short'" class="num" :class="signClass(row.ev_short_r)">{{ fmtR(row.ev_short_r) }}</span>
  <StatusBadge v-else-if="col === 'status'" :status="row.status" :side="row.active_signal_side" />
  <span v-else-if="col === 'limit'" class="num text-muted" :title="`max_per_lot ${fmtUsd(row.max_per_lot_usd)} · max_total ${fmtUsd(row.max_total_usd)}`">{{
    fmtUsdCompact(row.max_per_lot_usd)
  }}</span>
</template>
