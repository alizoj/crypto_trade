# Golden-фикстуры TradingView

Эталонные значения индикаторов с TradingView для `tests/indicators/test_ind_golden_tv.py`
(паритет, раздел 6 ТЗ). Пока CSV в этой папке нет, golden-тесты пропускаются.

## Как экспортировать

1. Откройте график **`BINANCE:<SYMBOL>.P`** (USDⓈ-M перп, тот же фид, что основной источник), например
   `BINANCE:BTCUSDT.P`. Для 1000-контрактов (`1000PEPEUSDT.P` и т.п.) цены в CSV остаются «как на
   графике» — тест считает наши индикаторы по OHLCV из того же файла, нормализация не нужна.
2. Таймфрейм: **1h** (обязательно) и по желанию 15m / 4h — отдельным файлом на каждый ТФ.
   VWAP сверяется только на внутридневных ТФ.
3. Сессия: по умолчанию (24x7, сутки с 00:00 UTC). Тип графика — обычные свечи (не Heikin Ashi),
   без «Adjust for dividends» и без продления сессии.
4. Добавьте на график скрипт ниже (Pine Editor → вставить → «Add to chart»). Другие индикаторы
   добавлять не нужно: всё выводит один скрипт, у каждой линии уникальный заголовок = имя колонки.
5. **Загрузите всю историю**: прокрутите график влево до самого первого бара (листинг) — TradingView
   считает индикаторы с первого загруженного бара, и OBV/ведущие na сверяются от него.
   Если всю историю загрузить нельзя, тесты значений всё равно работают (сравнение после прогрева
   25×period), но `obv` и `test_golden_warmup_pattern` могут не совпасть — такие колонки удалите.
6. Меню графика (стрелка рядом с названием лэйаута) → **Export chart data…** → Time format:
   **UNIX timestamp** (ISO тоже поддерживается) → Export.
7. Сохраните файл сюда как `BINANCE_<SYMBOL>.P_<TF>.csv`, например `BINANCE_BTCUSDT.P_60.csv`.
   Подходит любое имя с расширением `.csv`: каждый файл — отдельный набор тестов.

## Скрипт `golden_export` (Pine Script v6)

```pine
//@version=6
indicator("golden_export", overlay = false, max_bars_back = 5000)
plot(volume, "volume")
plot(ta.sma(close, 20), "sma_20")
plot(ta.ema(close, 20), "ema_20")
plot(ta.ema(close, 200), "ema_200")
plot(ta.rma(close, 14), "rma_14")
plot(ta.wma(close, 20), "wma_20")
plot(ta.stdev(close, 20), "stdev_20")
plot(ta.rsi(close, 14), "rsi_14")
plot(ta.tr(true), "tr")
plot(ta.atr(14), "atr_14")
[diPlus, diMinus, adxValue] = ta.dmi(14, 14)
plot(diPlus, "plus_di_14")
plot(diMinus, "minus_di_14")
plot(adxValue, "adx_14")
[bbBasis, bbUpper, bbLower] = ta.bb(close, 20, 2)
plot(bbBasis, "bb_basis_20")
plot(bbUpper, "bb_upper_20")
plot(bbLower, "bb_lower_20")
plot(ta.change(close), "change_1")
plot(ta.obv, "obv")
plot(ta.vwap(hlc3), "vwap")
plot(ta.pivothigh(high, 5, 5), "ph_5_5")
plot(ta.pivotlow(low, 5, 5), "pl_5_5")
```

## Формат CSV

| колонка | содержимое |
|---|---|
| `time` | время **открытия** бара: UNIX-секунды (или мс, или ISO 8601) |
| `open`, `high`, `low`, `close` | цены бара как на графике |
| `volume` | объём бара |
| `sma_20` … `pl_5_5` | значения линий скрипта; пусто / `NaN` = na |

Регистр заголовков не важен; лишние колонки игнорируются; при дублях берётся первая.

## Что проверяется

- `test_golden_values[<колонка>]` — относительный допуск **1e-6** после прогрева **25×period**
  (для RMA-индикаторов прогрева 10×period мало: (13/14)^140 ≈ 3·10⁻⁵).
- `test_golden_warmup_pattern` — число ведущих na у нас и у TradingView. Решает открытый вопрос
  seed у `ta.ema` (у нас — как встроенный: na первые n-1 баров, старт с SMA; `ema(..., seed="first")`
  — вариант `pine_ema` из справочника) и прогрев DMI (`ta.tr` без handle_na).
- `test_golden_pivots` — позиции и значения `ta.pivothigh/pivotlow(5, 5)` на баре подтверждения;
  заодно проверяет правило равенств (у нас: слева строго, справа нестрого).

Бары с нулевым объёмом (`zero_vol`) — как их обрабатывает TradingView (VWAP, RVOL), тоже видно
по этим фикстурам на неликвидном символе: стоит добавить хотя бы один такой файл.
