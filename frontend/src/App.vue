<template>
  <div class="min-h-screen bg-slate-50 flex">
    <!-- Sidebar -->
    <aside class="w-60 shrink-0 bg-white border-r border-slate-200 flex flex-col h-screen sticky top-0">
      <!-- Logo -->
      <div class="px-5 py-5 border-b border-slate-100">
        <div class="flex items-center gap-2.5">
          <div class="w-8 h-8 bg-gradient-to-br from-sky-400 to-sky-600 rounded-lg flex items-center justify-center">
            <svg
              viewBox="0 0 24 24"
              fill="none"
              class="w-5 h-5 text-white"
              stroke="currentColor"
              stroke-width="2"
            >
              <rect
                x="2"
                y="3"
                width="20"
                height="14"
                rx="2"
              />
              <path d="M8 21h8M12 17v4" />
            </svg>
          </div>
          <div>
            <div class="font-semibold text-slate-900 text-sm leading-none">
              S.L.O.P.
            </div>
            <div class="text-xs text-slate-400 mt-0.5">
              v5
            </div>
          </div>
        </div>
      </div>

      <!-- Platform status pill -->
      <div class="px-4 py-3 border-b border-slate-100">
        <div
          v-if="platformStore.isReady"
          class="flex items-center gap-2.5"
        >
          <span class="status-dot-green" />
          <span class="text-sm text-slate-700 font-semibold">{{ platformStore.domain }}</span>
        </div>
        <div
          v-else
          class="flex items-center gap-2 text-xs"
        >
          <span class="status-dot-yellow" />
          <RouterLink
            to="/setup"
            class="text-amber-600 font-medium hover:text-amber-700"
          >
            Setup required →
          </RouterLink>
        </div>
      </div>

      <!-- Navigation -->
      <nav class="flex-1 px-2 py-2 space-y-0 overflow-y-auto">
        <RouterLink
          v-for="item in navItems"
          :key="item.to"
          :to="item.to"
          :class="isActive(item.to) ? 'nav-link-active' : 'nav-link'"
        >
          <component
            :is="item.icon"
            class="w-4 h-4 shrink-0"
          />
          <span>{{ item.label }}</span>
        </RouterLink>
      </nav>

      <!-- Bottom: health summary -->
      <div class="px-4 py-3 border-t border-slate-100">
        <div class="flex items-center justify-between text-xs text-slate-400">
          <span>{{ healthSummary }}</span>
          <RouterLink
            to="/health"
            class="hover:text-sky-500 font-medium"
          >
            Health
          </RouterLink>
        </div>
      </div>
    </aside>

    <!-- Main -->
    <main
      class="flex-1 min-w-0 overflow-y-auto"
      style="scrollbar-gutter: stable"
    >
      <!-- Notification banner -->
      <Transition name="slide-down">
        <div
          v-if="notification"
          :class="['px-6 py-3 text-sm font-medium flex items-center justify-between',
                   notification.type === 'error' ? 'bg-red-50 text-red-700 border-b border-red-100' :
                   notification.type === 'success' ? 'bg-green-50 text-green-700 border-b border-green-100' :
                   'bg-sky-50 text-sky-700 border-b border-sky-100']"
        >
          <span>{{ notification.message }}</span>
          <button
            class="opacity-60 hover:opacity-100 ml-4"
            @click="notification = null"
          >
            ✕
          </button>
        </div>
      </Transition>

      <RouterView />
    </main>

    <!-- Global toast notification stack -->
    <ToastContainer />
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, watch, h } from 'vue'
import { catalog, apps as appsApi, health as healthApi, infra as infraApi, routing as routingApi } from './api/client'
import { setCatalogCache, setInstalledCache } from './catalogCache'
import { setAppsCache, setHealthCache, setInfraSlotsCache, setRoutingCache } from './appCache'
import { RouterLink, RouterView, useRoute } from 'vue-router'
import { usePlatformStore } from './stores/platform'
import ToastContainer from './components/ToastContainer.vue'

const platformStore = usePlatformStore()
const route = useRoute()
const notification = ref<{ type: string; message: string } | null>(null)
const healthCounts = ref({ ok: 0, warning: 0, error: 0 })

const healthSummary = computed(() => {
  const { ok, warning, error } = healthCounts.value
  const total = ok + warning + error
  if (total === 0) return 'No apps installed'
  if (error > 0) return `${error} unhealthy`
  if (warning > 0) return `${warning} warnings`
  return `${ok} apps healthy`
})

// Simple SVG icon components
const icons = {
  grid: () => h('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2' },
    [h('rect', { x: '3', y: '3', width: '7', height: '7' }),
     h('rect', { x: '14', y: '3', width: '7', height: '7' }),
     h('rect', { x: '3', y: '14', width: '7', height: '7' }),
     h('rect', { x: '14', y: '14', width: '7', height: '7' })]),
  box: () => h('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2' },
    [h('path', { d: 'M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z' })]),
  server: () => h('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2' },
    [h('rect', { x: '2', y: '2', width: '20', height: '8', rx: '2' }),
     h('rect', { x: '2', y: '14', width: '20', height: '8', rx: '2' }),
     h('line', { x1: '6', y1: '6', x2: '6.01', y2: '6' }),
     h('line', { x1: '6', y1: '18', x2: '6.01', y2: '18' })]),
  route: () => h('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2' },
    [h('circle', { cx: '6', cy: '19', r: '3' }),
     h('path', { d: 'M9 19h8.5a3.5 3.5 0 0 0 0-7h-11a3.5 3.5 0 0 1 0-7H15' }),
     h('circle', { cx: '18', cy: '5', r: '3' })]),
  hdd: () => h('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2' },
    [h('line', { x1: '22', y1: '12', x2: '2', y2: '12' }),
     h('path', { d: 'M5.45 5.11L2 12v6a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2v-6l-3.45-6.89A2 2 0 0 0 16.76 4H7.24a2 2 0 0 0-1.79 1.11z' }),
     h('line', { x1: '6', y1: '16', x2: '6.01', y2: '16' }),
     h('line', { x1: '10', y1: '16', x2: '10.01', y2: '16' })]),
  brain: () => h('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2' },
    [h('path', { d: 'M9.5 2A2.5 2.5 0 0 1 12 4.5v15a2.5 2.5 0 0 1-4.96-.46 2.5 2.5 0 0 1-2.96-3.08 3 3 0 0 1-.34-5.58 2.5 2.5 0 0 1 1.32-4.24 2.5 2.5 0 0 1 1.98-3A2.5 2.5 0 0 1 9.5 2Z' }),
     h('path', { d: 'M14.5 2A2.5 2.5 0 0 0 12 4.5v15a2.5 2.5 0 0 0 4.96-.46 2.5 2.5 0 0 0 2.96-3.08 3 3 0 0 0 .34-5.58 2.5 2.5 0 0 0-1.32-4.24 2.5 2.5 0 0 0-1.98-3A2.5 2.5 0 0 0 14.5 2Z' })]),
  activity: () => h('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2' },
    [h('polyline', { points: '22 12 18 12 15 21 9 3 6 12 2 12' })]),
  coverage: () => h('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2' }, [
    h('path', { d: 'M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z' }),
  ]),
  eye: () => h('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2' }, [
    h('path', { d: 'M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z' }),
    h('circle', { cx: '12', cy: '12', r: '3' }),
  ]),
  gear: () => h('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2' },
    [h('circle', { cx: '12', cy: '12', r: '3' }),
     h('path', { d: 'M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z' })]),
  wrench: () => h('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2' },
    [h('path', { d: 'M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.77 3.77z' })]),
  chat: () => h('svg', { viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', 'stroke-width': '2' },
    [h('path', { d: 'M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z' })]),
}

const navItems = computed(() => [
  { to: '/', label: 'Dashboard', icon: icons.grid },
  { to: '/catalog', label: 'Catalog', icon: icons.box },
  { to: '/infrastructure', label: 'Infrastructure', icon: icons.server },
  { to: '/routing', label: 'Routing', icon: icons.route },
  { to: '/storage', label: 'Storage', icon: icons.hdd },
  { to: '/models', label: 'LLM Models', icon: icons.brain },
  { to: '/health', label: 'Health', icon: icons.activity },
  { to: '/chat', label: 'Agent Chat', icon: icons.chat },
  { to: '/observability', label: 'Observability', icon: icons.eye },
  { to: '/coverage', label: 'Coverage', icon: icons.coverage },
  { to: '/settings', label: 'Settings', icon: icons.gear },
  ...(!platformStore.isReady ? [{ to: '/setup', label: 'Setup', icon: icons.wrench }] : []),
])

function isActive(to: string) {
  if (to === '/') return route.path === '/'
  return route.path.startsWith(to)
}

async function loadHealthSummary() {
  try {
    const res = await fetch('/api/v1/health/summary')
    if (res.ok) {
      const d = await res.json()
      healthCounts.value = { ok: d.ok ?? 0, warning: d.warning ?? 0, error: d.error ?? 0 }
    }
  } catch { /* offline */ }
}

// Refresh platform status on every route change — catches external resets (curl/API)
// and ensures the sidebar domain/status is always current
watch(route, async () => {
  await platformStore.fetchStatus()
}, { immediate: false })

onMounted(async () => {
  await platformStore.fetchStatus()
  await loadHealthSummary()
  setInterval(loadHealthSummary, 60_000)

  // Prefetch all shared page data in background — views read from cache
  // and render instantly on navigation. Each fetch is independent so a
  // single slow endpoint doesn't block the others.
  appsApi.list().then(list => {
    setAppsCache(list)
    setInstalledCache(new Set(list.map(a => a.key)))
  }).catch(() => {})
  catalog.all().then(data => setCatalogCache(data)).catch(() => {})
  healthApi.allApps().then(data => setHealthCache(data)).catch(() => {})
  infraApi.slots().then(data => setInfraSlotsCache(data)).catch(() => {})
  routingApi.media().then(data => setRoutingCache(data)).catch(() => {})

  // Redirect to setup if not ready
  if (!platformStore.isReady && route.path !== '/setup') {
    // Only suggest, don't force redirect
  }
})
</script>

<style>
.slide-down-enter-active, .slide-down-leave-active { transition: all 0.2s ease; }
.slide-down-enter-from, .slide-down-leave-to { opacity: 0; transform: translateY(-100%); }
</style>
