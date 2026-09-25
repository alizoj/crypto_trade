// Метрики прогонов бэктеста: подписи, форматирование, «лучшее» значение для сравнения.
import type { BacktestMetrics, BacktestRun } from '@/api/types'
import { fmtNum, fmtShare, fmtUsd } from './format'

export type MetricKey = keyof BacktestMetrics

export interface MetricDef {
  key: MetricKey
  label: string
  title: string
  /** больше — лучше (true), меньше — лучше (false), нейтрально (null) */
  higherBetter: boolean | null
  fmt: (v: number | undefined) => string
}

const share = (v: number | undefined) => fmtShare(v ?? null, 1)
const days = (v: number | undefined) => (v === undefined ? '—' : `${fmtNum(v, 0)} д`)

export const METRICS: MetricDef[] = [
  { key: 'p_pass_s1', label: 'P(pass) S1', title: 'Вероятность пройти стадию 1 в горизонте H', higherBetter: true, fmt: share },
  { key: 'p_pass_s2', label: 'P(pass) S2', title: 'Вероятность пройти стадию 2', higherBetter: true, fmt: share },
  { key: 'p_pass_both', label: 'P(pass) обе', title: 'Вероятность пройти обе стадии', higherBetter: true, fmt: share },
  { key: 'p_fail_daily', label: 'Fail daily', title: 'Доля провалов по дневному лимиту', higherBetter: false, fmt: share },
  { key: 'p_fail_total', label: 'Fail total', title: 'Доля провалов по общему лимиту', higherBetter: false, fmt: share },
  { key: 'p_unresolved', label: 'Unresolved', title: 'Не завершились в горизонте H', higherBetter: null, fmt: share },
  { key: 'median_days_s1', label: 'Медиана дней S1', title: 'Медианное время прохождения стадии 1', higherBetter: false, fmt: days },
  { key: 'p90_days_s1', label: 'P90 дней S1', title: '90-й перцентиль времени прохождения стадии 1', higherBetter: false, fmt: days },
  { key: 'ev_usd', label: 'EV, $', title: 'Ожидаемая ценность челленджа с учётом цены', higherBetter: true, fmt: (v) => fmtUsd(v ?? null, 1, true) },
  { key: 'pf', label: 'PF', title: 'Profit factor', higherBetter: true, fmt: (v) => fmtNum(v ?? null, 2) },
  { key: 'winrate', label: 'Winrate', title: 'Доля прибыльных сделок', higherBetter: true, fmt: share },
  { key: 'avg_r', label: 'Ср. R', title: 'Средний результат сделки в R', higherBetter: true, fmt: (v) => fmtNum(v ?? null, 3, { sign: true }) },
  { key: 'n_trades', label: 'Сделок', title: 'Число сделок', higherBetter: null, fmt: (v) => fmtNum(v ?? null, 0, { group: true }) },
  { key: 'dsr', label: 'DSR', title: 'Deflated Sharpe Ratio (поправка на множественное тестирование)', higherBetter: true, fmt: (v) => fmtNum(v ?? null, 2) },
  { key: 'pbo', label: 'PBO', title: 'Probability of Backtest Overfitting', higherBetter: false, fmt: (v) => fmtNum(v ?? null, 2) },
]

/** id прогона(ов) с лучшим значением метрики среди сравниваемых; пусто, если метрика нейтральна или значений < 2. */
export function bestRunIds(runs: BacktestRun[], def: MetricDef): Set<number> {
  const out = new Set<number>()
  if (def.higherBetter === null) return out
  const vals = runs.map((r) => ({ id: r.id, v: r.metrics[def.key] })).filter((x): x is { id: number; v: number } => typeof x.v === 'number')
  if (vals.length < 2) return out
  const best = def.higherBetter ? Math.max(...vals.map((x) => x.v)) : Math.min(...vals.map((x) => x.v))
  for (const x of vals) if (x.v === best) out.add(x.id)
  return out
}
