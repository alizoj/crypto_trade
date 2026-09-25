<script setup lang="ts">
import { computed, ref } from 'vue'
import type { AssetClass, Timeframe } from '@/api/types'
import { useScreenerStore } from '@/stores/screener'
import { useMetaStore } from '@/stores/meta'
import { ASSET_CLASS_RU } from '@/utils/labels'
import { isDefaultFilters, type SideFilter } from '@/utils/screenerFilters'

const props = defineProps<{ compact?: boolean }>()

const screener = useScreenerStore()
const meta = useMetaStore()
const expanded = ref(false)
const showAll = computed(() => !props.compact || expanded.value)
const activeCount = computed(() => {
  const x = screener.filters
  return (x.classes.length ? 1 : 0) + (x.onlySignals ? 1 : 0) + (x.minP !== null ? 1 : 0) + (x.minEv !== null ? 1 : 0) + (x.side !== 'both' ? 1 : 0) + (x.search.trim() ? 1 : 0)
})

const presetName = ref('')
const selectedPreset = ref('')
const showSave = ref(false)

const f = computed(() => screener.filters)

function toggleClass(c: AssetClass): void {
  const cur = f.value.classes
  screener.setFilters({ ...f.value, classes: cur.includes(c) ? cur.filter((x) => x !== c) : [...cur, c] })
}

function setSide(s: SideFilter): void {
  screener.setFilters({ ...f.value, side: s })
}

function setNumber(key: 'minP' | 'minEv', e: Event): void {
  const raw = (e.target as HTMLInputElement).value.trim().replace(',', '.')
  const v = raw === '' ? null : Number(raw)
  screener.setFilters({ ...f.value, [key]: v === null || !Number.isFinite(v) ? null : v })
}

function setOnlySignals(e: Event): void {
  screener.setFilters({ ...f.value, onlySignals: (e.target as HTMLInputElement).checked })
}

function setSearch(e: Event): void {
  screener.setFilters({ ...f.value, search: (e.target as HTMLInputElement).value })
}

function setTf(tf: Timeframe): void {
  screener.setTf(tf)
}

function savePreset(): void {
  const name = presetName.value.trim()
  if (!name) return
  screener.savePreset(name)
  selectedPreset.value = name
  presetName.value = ''
  showSave.value = false
}

function applyPreset(e: Event): void {
  const name = (e.target as HTMLSelectElement).value
  selectedPreset.value = name
  if (name) screener.applyPreset(name)
}

function deletePreset(): void {
  if (!selectedPreset.value) return
  screener.deletePreset(selectedPreset.value)
  selectedPreset.value = ''
}

const sides: { v: SideFilter; label: string }[] = [
  { v: 'both', label: 'Обе' },
  { v: 'long', label: 'Лонг' },
  { v: 'short', label: 'Шорт' },
]
</script>

<template>
  <section class="card flex flex-wrap items-end gap-x-4 gap-y-3 p-3" aria-label="Фильтры скринера">
    <div>
      <span class="label">Таймфрейм</span>
      <div class="seg" role="group" aria-label="Таймфрейм">
        <button
          v-for="tf in meta.timeframes"
          :key="tf"
          type="button"
          :aria-pressed="screener.tf === tf"
          :data-testid="`tf-${tf}`"
          @click="setTf(tf)"
        >
          {{ tf }}
        </button>
      </div>
    </div>

    <button
      v-if="compact"
      type="button"
      class="btn ml-auto"
      :aria-expanded="expanded"
      data-testid="filters-toggle"
      @click="expanded = !expanded"
    >
      Фильтры<template v-if="activeCount"> ({{ activeCount }})</template> {{ expanded ? '▴' : '▾' }}
    </button>

    <template v-if="showAll">
    <div class="min-w-0">
      <span class="label">Класс актива</span>
      <div class="flex flex-wrap gap-1">
        <button
          v-for="c in meta.assetClasses"
          :key="c"
          type="button"
          class="btn min-h-8 px-2.5 text-[13px]"
          :class="f.classes.includes(c) ? 'btn-active' : ''"
          :aria-pressed="f.classes.includes(c)"
          :data-testid="`class-${c}`"
          @click="toggleClass(c)"
        >
          {{ ASSET_CLASS_RU[c] }}
        </button>
      </div>
    </div>

    <div>
      <span class="label">Сторона</span>
      <div class="seg" role="group" aria-label="Сторона">
        <button
          v-for="s in sides"
          :key="s.v"
          type="button"
          :aria-pressed="f.side === s.v"
          :data-testid="`side-${s.v}`"
          :title="s.v === 'both' ? 'Обе стороны' : `Только ${s.label.toLowerCase()}: сторона активного сигнала, иначе больший score; пороги P/EV — по этой стороне`"
          @click="setSide(s.v)"
        >
          {{ s.label }}
        </button>
      </div>
    </div>

    <div class="w-24">
      <label class="label" for="flt-minp">Мин. P, %</label>
      <input
        id="flt-minp"
        class="input num w-full"
        type="number"
        min="0"
        max="100"
        step="1"
        placeholder="—"
        :value="f.minP ?? ''"
        data-testid="min-p"
        title="Учитываются только откалиброванные вероятности с достаточным n"
        @change="setNumber('minP', $event)"
      />
    </div>
    <div class="w-24">
      <label class="label" for="flt-minev">Мин. EV, R</label>
      <input
        id="flt-minev"
        class="input num w-full"
        type="number"
        step="0.05"
        placeholder="—"
        :value="f.minEv ?? ''"
        data-testid="min-ev"
        @change="setNumber('minEv', $event)"
      />
    </div>

    <label class="flex min-h-8 cursor-pointer items-center gap-2 select-none">
      <input type="checkbox" class="h-4 w-4" :checked="f.onlySignals" data-testid="only-signals" @change="setOnlySignals" />
      Только сигналы
    </label>

    <div class="w-36">
      <label class="label" for="flt-search">Поиск</label>
      <input id="flt-search" class="input w-full" type="search" placeholder="BTC, XAU…" :value="f.search" data-testid="search" @input="setSearch" />
    </div>

    <button type="button" class="btn btn-sm" :disabled="isDefaultFilters(f)" data-testid="reset-filters" @click="screener.resetFilters()">
      Сбросить
    </button>

    <div class="flex flex-wrap items-end gap-1">
      <div>
        <label class="label" for="flt-preset">Пресет</label>
        <select id="flt-preset" class="input py-1 text-sm" :value="selectedPreset" data-testid="preset-select" @change="applyPreset">
          <option value="">— выбрать —</option>
          <option v-for="p in screener.presets" :key="p.name" :value="p.name">{{ p.name }}</option>
        </select>
      </div>
      <button v-if="selectedPreset" type="button" class="btn btn-sm" title="Удалить пресет" @click="deletePreset">✕</button>
      <template v-if="showSave">
        <input
          v-model="presetName"
          class="input w-32 py-1 text-sm"
          placeholder="Название"
          maxlength="40"
          data-testid="preset-name"
          aria-label="Название пресета"
          @keydown.enter.prevent="savePreset"
        />
        <button type="button" class="btn btn-sm btn-primary" :disabled="!presetName.trim()" data-testid="preset-save" @click="savePreset">
          Сохранить
        </button>
      </template>
      <button v-else type="button" class="btn btn-sm" data-testid="preset-new" @click="showSave = true">Сохранить фильтры…</button>
    </div>
    </template>
  </section>
</template>
