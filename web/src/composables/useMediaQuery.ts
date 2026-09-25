import { onBeforeUnmount, ref, type Ref } from 'vue'

/** Реактивный matchMedia (без window — false). */
export function useMediaQuery(query: string): Ref<boolean> {
  const matches = ref(false)
  let mql: MediaQueryList | null = null
  const update = () => {
    matches.value = mql?.matches ?? false
  }
  try {
    mql = window.matchMedia(query)
    update()
    mql.addEventListener('change', update)
  } catch {
    mql = null
  }
  onBeforeUnmount(() => mql?.removeEventListener('change', update))
  return matches
}
