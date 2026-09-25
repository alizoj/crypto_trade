<script setup lang="ts">
import type { Account, TradePlan } from '@/api/types'
import { fmtDateTime, fmtNum, fmtPct, fmtPrice, fmtQty, fmtUsd, type TzMode } from '@/utils/format'

defineProps<{ plan: TradePlan | null; account: Account | null; tz: TzMode }>()

function slPct(p: TradePlan): number {
  return (Math.abs(p.entry - p.sl) / p.entry) * 100
}
</script>

<template>
  <div v-if="!plan" class="text-muted-sm">План не рассчитан</div>
  <div v-else class="space-y-2 text-[13px]" :data-testid="`plan-${plan.side}`">
    <p
      v-if="plan.blocked_reason"
      class="rounded-md border border-short/40 bg-short/10 px-2 py-1.5 text-short"
      role="note"
      data-testid="plan-blocked"
    >
      <b>Заблокировано:</b> {{ plan.blocked_reason }}
    </p>
    <dl class="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
      <dt class="text-muted">Вход</dt>
      <dd class="num">
        <b data-field="entry">{{ fmtPrice(plan.entry) }}</b> <span class="text-muted">{{ plan.entry_type === 'limit' ? 'limit' : 'market' }}</span>
      </dd>
      <dt class="text-muted">SL</dt>
      <dd class="num">
        <b class="text-short" data-field="sl">{{ fmtPrice(plan.sl) }}</b>
        <span class="text-muted"> stop-market · триггер {{ plan.sl_trigger === 'mark' ? 'Mark' : 'Last' }} · {{ fmtPct(slPct(plan), 2) }}</span>
      </dd>
      <dt class="text-muted">TP1</dt>
      <dd class="num">
        <b class="text-long" data-field="tp1">{{ fmtPrice(plan.tp1) }}</b>
        <span class="text-muted"> · {{ fmtNum(plan.tp1_frac * 100, 0) }}% позиции</span>
      </dd>
      <template v-if="plan.tp2 !== null">
        <dt class="text-muted">TP2</dt>
        <dd class="num"><b class="text-long" data-field="tp2">{{ fmtPrice(plan.tp2) }}</b></dd>
      </template>
      <dt class="text-muted">R:R</dt>
      <dd class="num" data-field="rr">{{ fmtNum(plan.rr1, 2) }}<template v-if="plan.rr2 !== null"> / {{ fmtNum(plan.rr2, 2) }}</template></dd>
      <template v-if="plan.account_id !== null">
        <dt class="text-muted">Размер</dt>
        <dd class="num">
          <b data-field="notional">{{ fmtUsd(plan.notional_usd) }}</b> notional · {{ fmtQty(plan.qty) }} шт.
        </dd>
        <dt class="text-muted">Риск</dt>
        <dd class="num">
          <b data-field="risk">{{ fmtUsd(plan.risk_usd, 2) }}</b>
          <span class="text-muted">
            · {{ fmtPct(plan.risk_pct_daily_room, 1) }} дневного, {{ fmtPct(plan.risk_pct_total_room, 1) }} общего запаса</span
          >
        </dd>
      </template>
      <template v-else>
        <dt class="text-muted">Размер</dt>
        <dd class="text-muted">выберите счёт, чтобы рассчитать размер и риск</dd>
      </template>
      <dt class="text-muted">До</dt>
      <dd class="num">{{ fmtDateTime(plan.expires_at, tz, { year: false }) }}</dd>
    </dl>
    <p v-if="account && plan.account_id !== null && plan.account_id !== account.id" class="text-muted-sm">
      Размер рассчитан для счёта #{{ plan.account_id }}
    </p>
    <p class="text-muted-sm">
      SL — только stop-market: limit-SL может не исполниться при проскальзывании. Лимитный выход и SL/TP на одном объёме не совмещать.
    </p>
  </div>
</template>
