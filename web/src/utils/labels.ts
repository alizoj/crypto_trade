// Русские подписи для значений перечислений из контракта.
import type {
  AccountStatus,
  AssetClass,
  BacktestKind,
  ChallengeType,
  Group,
  ScreenerStatus,
  Side,
  SignalStatus,
  Stage,
} from '@/api/types'

export const ASSET_CLASS_RU: Record<AssetClass, string> = {
  crypto: 'Крипто',
  equity: 'Акция',
  etf: 'ETF',
  metal: 'Металл',
  energy: 'Энергия',
}

export const GROUP_RU: Record<Group, string> = {
  crypto_major: 'Крипто major',
  crypto_alt: 'Крипто alt',
  tradfi: 'TradFi',
}

export const SIDE_RU: Record<Side, string> = { long: 'Лонг', short: 'Шорт' }

export const SCREENER_STATUS_RU: Record<ScreenerStatus, string> = {
  signal: 'Сигнал',
  watch: 'Наблюдение',
  none: 'Нет',
  off_session: 'Вне сессии',
  halted: 'Halted',
}

export const SIGNAL_STATUS_RU: Record<SignalStatus, string> = {
  new: 'Новый',
  active: 'Активен',
  triggered: 'Вход сработал',
  expired: 'Истёк',
  invalidated: 'Отменён',
  tp: 'TP',
  sl: 'SL',
  timeout: 'Таймаут',
}

export const STAGE_RU: Record<Stage, string> = { stage1: 'Стадия 1', stage2: 'Стадия 2', trader: 'Трейдер' }

export const CHALLENGE_RU: Record<ChallengeType, string> = {
  two_stage: 'Двухстадийный',
  one_stage: 'Одностадийный',
}

export const ACCOUNT_STATUS_RU: Record<AccountStatus, string> = {
  active: 'Активен',
  passed: 'Пройден',
  failed: 'Провален',
}

export const BACKTEST_KIND_RU: Record<BacktestKind, string> = {
  backtest: 'Бэктест',
  walkforward: 'Walk-forward',
  holdout: 'Hold-out',
  sanity: 'Санити-чек',
}

/** Размеры счетов по правилам HashHedge (раздел 2.1). */
export const ACCOUNT_SIZES: Record<ChallengeType, number[]> = {
  two_stage: [5000, 10000, 25000, 50000, 100000, 200000],
  one_stage: [5000, 10000, 25000, 50000, 100000],
}

export const OUTCOME_RU = { tp: 'TP', sl: 'SL', timeout: 'Таймаут' } as const
