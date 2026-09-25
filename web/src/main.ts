import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { router } from './router'
import { setUnauthorizedHandler } from './api/client'
import { useAuthStore } from './stores/auth'
import { useScreenerStore } from './stores/screener'
import './style.css'

async function bootstrap(): Promise<void> {
  if (import.meta.env.VITE_MOCK === '1') {
    const { installMock } = await import('./mock')
    installMock()
  }

  const app = createApp(App)
  const pinia = createPinia()
  app.use(pinia)
  app.use(router)

  // 401 от любого запроса → на экран логина (с возвратом на текущую страницу).
  setUnauthorizedHandler(() => {
    const auth = useAuthStore(pinia)
    auth.markLoggedOut()
    useScreenerStore(pinia).stop()
    const cur = router.currentRoute.value
    if (cur.name !== 'login') {
      void router.replace({ name: 'login', query: cur.fullPath && cur.fullPath !== '/' ? { redirect: cur.fullPath } : {} })
    }
  })

  await router.isReady()
  app.mount('#app')
}

void bootstrap()
