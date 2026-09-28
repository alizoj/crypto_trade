import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'
import { useAuthStore } from '@/stores/auth'

declare module 'vue-router' {
  interface RouteMeta {
    public?: boolean
    title?: string
  }
}

const routes: RouteRecordRaw[] = [
  { path: '/login', name: 'login', component: () => import('@/views/LoginView.vue'), meta: { public: true, title: 'Вход' } },
  { path: '/', name: 'screener', component: () => import('@/views/ScreenerView.vue'), meta: { title: 'Скринер' } },
  {
    path: '/instrument/:symbol',
    name: 'instrument',
    component: () => import('@/views/InstrumentView.vue'),
    props: true,
    meta: { title: 'Инструмент' },
  },
  { path: '/accounts', name: 'accounts', component: () => import('@/views/AccountsView.vue'), meta: { title: 'Счета' } },
  { path: '/signals', name: 'signals', component: () => import('@/views/SignalsView.vue'), meta: { title: 'Журнал сигналов' } },
  { path: '/journal', name: 'journal', component: () => import('@/views/JournalView.vue'), meta: { title: 'Журнал сделок' } },
  { path: '/backtests', name: 'backtests', component: () => import('@/views/BacktestsView.vue'), meta: { title: 'Бэктесты' } },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

export const router = createRouter({
  history: createWebHistory(import.meta.env.BASE_URL),
  routes,
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach(async (to) => {
  const auth = useAuthStore()
  const ok = await auth.check()
  if (to.meta.public) {
    if (to.name === 'login' && ok) {
      const redirect = typeof to.query.redirect === 'string' && to.query.redirect.startsWith('/') ? to.query.redirect : '/'
      return redirect
    }
    return true
  }
  if (!ok) return { name: 'login', query: to.fullPath !== '/' ? { redirect: to.fullPath } : {} }
  return true
})

router.afterEach((to) => {
  const sym = typeof to.params.symbol === 'string' ? `${to.params.symbol} · ` : ''
  document.title = `${sym}${to.meta.title ?? ''} — HashHedge Quant`
})
