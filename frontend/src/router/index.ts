// src/router/index.ts
import { createRouter, createWebHistory } from 'vue-router'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      component: () => import('../views/DashboardView.vue'),
      meta: { title: 'Dashboard' }
    },
    {
      path: '/setup',
      component: () => import('../views/SetupView.vue'),
      meta: { title: 'Platform Setup' }
    },
    {
      path: '/catalog',
      component: () => import('../views/CatalogView.vue'),
      meta: { title: 'Catalog' }
    },
    {
      path: '/apps/:key',
      component: () => import('../views/AppDetailView.vue'),
      meta: { title: 'App Detail' }
    },
    {
      path: '/infrastructure',
      component: () => import('../views/InfraView.vue'),
      meta: { title: 'Infrastructure' }
    },
    {
      path: '/routing',
      component: () => import('../views/RoutingView.vue'),
      meta: { title: 'Request Routing' }
    },
    {
      path: '/storage',
      component: () => import('../views/StorageView.vue'),
      meta: { title: 'Storage' }
    },
    {
      path: '/models',
      component: () => import('../views/ModelsView.vue'),
      meta: { title: 'LLM Models' }
    },
    {
      path: '/health',
      component: () => import('../views/HealthView.vue'),
      meta: { title: 'Health' }
    },
    {
      path: '/chat',
      component: () => import('../views/ChatView.vue'),
      meta: { title: 'Agent Chat' }
    },
    {
      path: '/observability',
      component: () => import('../views/ObservabilityView.vue'),
      meta: { title: 'Observability' }
    },
    {
      path: '/coverage',
      component: () => import('../views/CoverageView.vue'),
      meta: { title: 'Coverage' }
    },
    {
      path: '/settings',
      component: () => import('../views/SettingsView.vue'),
      meta: { title: 'Settings' }
    },
  ]
})

router.afterEach((to) => {
  document.title = to.meta.title ? `${to.meta.title} — S.L.O.P.` : 'S.L.O.P.'
})

export default router
