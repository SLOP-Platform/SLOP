<template>
  <div class="p-4 max-w-4xl mx-auto w-full">
    <!-- Header -->
    <div class="flex items-center justify-between mb-6">
      <div>
        <h1 class="page-title">
          Dashboard
        </h1>
        <p class="page-subtitle flex items-center gap-2">
          <span>{{ platformStore.isReady ? platformStore.domain : 'Platform not configured' }}</span>
          <span
            v-if="platformStore.isReady"
            :class="['text-xs px-2 py-0.5 rounded-full font-medium',
                     traefikRunning ? 'bg-green-100 text-green-700' : 'bg-red-100 text-red-600']"
          >
            Traefik {{ traefikRunning ? '✓' : '✗' }}
          </span>
        </p>
      </div>
      <button
        :disabled="loading"
        class="btn-secondary btn-sm"
        @click="refreshAll"
      >
        <svg
          v-if="!loading"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          class="w-4 h-4"
        >
          <polyline points="23 4 23 10 17 10" /><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10" />
        </svg>
        <svg
          v-else
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2"
          class="w-4 h-4 animate-spin"
        >
          <line
            x1="12"
            y1="2"
            x2="12"
            y2="6"
          /><line
            x1="12"
            y1="18"
            x2="12"
            y2="22"
          />
          <line
            x1="4.93"
            y1="4.93"
            x2="7.76"
            y2="7.76"
          /><line
            x1="16.24"
            y1="16.24"
            x2="19.07"
            y2="19.07"
          />
        </svg>
        Refresh
      </button>
    </div>

    <!-- Setup prompt -->
    <div
      v-if="!platformStore.isReady"
      class="card mb-6"
    >
      <div class="card-body flex items-start gap-4">
        <div class="w-10 h-10 bg-amber-100 rounded-xl flex items-center justify-center shrink-0">
          <svg
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            class="w-5 h-5 text-amber-600"
          >
            <circle
              cx="12"
              cy="12"
              r="10"
            /><line
              x1="12"
              y1="8"
              x2="12"
              y2="12"
            /><line
              x1="12"
              y1="16"
              x2="12.01"
              y2="16"
            />
          </svg>
        </div>
        <div>
          <h3 class="font-semibold text-slate-900">
            Platform setup required
          </h3>
          <p class="text-sm text-slate-500 mt-1">
            Complete the setup wizard to deploy Traefik, configure your domain, and start installing apps.
          </p>
          <RouterLink
            to="/setup"
            class="btn-primary btn-sm mt-3 inline-flex"
          >
            Start setup →
          </RouterLink>
        </div>
      </div>
    </div>

    <!-- Stats row -->
    <div class="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
      <div
        v-for="stat in stats"
        :key="stat.label"
        class="card card-body"
      >
        <div
          class="text-2xl font-bold"
          :class="stat.color"
        >
          {{ stat.value }}
        </div>
        <div class="text-sm text-slate-500 mt-0.5">
          {{ stat.label }}
        </div>
      </div>
    </div>

    <!-- SLOP Agent system widget — tier-0, always pinned above catalog apps -->
    <div
      v-if="agentChecks.length"
      class="mb-4"
    >
      <div class="flex items-center gap-2 mb-2">
        <span class="text-xs font-medium text-slate-400 uppercase tracking-wide">System</span>
      </div>
      <div
        class="card card-body border-l-2"
        :class="agentChecks[0]?.status === 'running' ? 'border-l-green-400' :
          agentChecks[0]?.status === 'error' ? 'border-l-red-400' :
          agentChecks[0]?.status === 'disabled' ? 'border-l-slate-300' :
          'border-l-amber-300'"
      >
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-3">
            <div class="w-9 h-9 bg-slate-100 rounded-lg flex items-center justify-center shrink-0 text-lg">
              ⚡
            </div>
            <div>
              <div class="font-medium text-slate-900 text-sm">
                SLOP Agent
              </div>
              <div class="text-xs text-slate-400">
                System monitor &amp; remediator
              </div>
            </div>
          </div>
          <div class="flex items-center gap-2">
            <span
              class="badge text-xs"
              :class="agentChecks[0]?.status === 'running' ? 'badge-green' :
                agentChecks[0]?.status === 'error' ? 'badge-red' :
                agentChecks[0]?.status === 'disabled' ? 'badge-gray' :
                'badge-yellow'"
            >
              {{ agentChecks[0]?.status ?? 'unknown' }}
            </span>
            <span class="badge badge-gray text-xs">system</span>
          </div>
        </div>
        <div
          v-if="agentChecks[0]?.summary"
          class="mt-2 text-xs text-slate-500"
        >
          {{ agentChecks[0].summary }}
        </div>
      </div>
    </div>

    <!-- Installed apps -->
    <div class="mb-6">
      <div class="flex items-center justify-between mb-3">
        <h2 class="font-semibold text-slate-900">
          Installed Apps
        </h2>
      </div>

      <div
        v-if="installedApps.length === 0 && !loading"
        class="card card-body text-center py-12"
      >
        <div class="text-slate-400 text-sm">
          No apps installed yet.
        </div>
      </div>

      <div
        v-if="loading && installedApps.length === 0"
        class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3"
      >
        <div
          v-for="n in 3"
          :key="n"
          class="card card-body animate-pulse"
        >
          <div class="flex items-start gap-3">
            <div class="w-9 h-9 bg-slate-100 rounded-lg shrink-0" />
            <div class="flex-1 space-y-1.5">
              <div class="h-3 bg-slate-100 rounded w-1/2" /><div class="h-2.5 bg-slate-100 rounded w-1/3" />
            </div>
          </div>
        </div>
      </div>
      <div
        v-else
        class="grid gap-3 sm:grid-cols-2 lg:grid-cols-3"
      >
        <RouterLink
          v-for="app in installedApps"
          :key="app.key"
          :to="`/apps/${app.key}`"
          class="card card-body hover:shadow-md transition-shadow duration-150 cursor-pointer"
        >
          <div class="flex items-start justify-between">
            <div class="flex items-center gap-3">
              <div class="w-9 h-9 bg-slate-100 rounded-lg flex items-center justify-center overflow-hidden">
                <img
                  :src="iconUrl(app.key)"
                  :alt="app.display_name"
                  class="w-7 h-7 object-contain"
                  @error="(e: Event) => { const t = e.target as HTMLImageElement; if(t){t.style.display='none'; const s=t.nextElementSibling as HTMLElement; if(s) s.style.display='block'} }"
                >
                <span class="text-lg leading-none hidden">{{ getCatalogIcon(app.key) }}</span>
              </div>
              <div>
                <div class="font-medium text-slate-900 text-sm">
                  {{ app.display_name }}
                </div>
                <div class="text-xs text-slate-400 capitalize">
                  {{ app.category }}
                </div>
              </div>
            </div>
            <div class="flex flex-col items-end gap-1">
              <span :class="statusBadge(app.status)">{{ app.status }}</span>
              <span
                v-if="app.category === 'custom'"
                class="badge badge-yellow text-xs"
              >custom</span>
            </div>
          </div>

          <!-- Health checks for this app -->
          <div
            v-if="appHealth[app.key]?.length"
            class="mt-3 space-y-1"
          >
            <div
              v-for="check in appHealth[app.key]"
              :key="check.check_name"
              class="flex items-center gap-2 text-xs text-slate-500"
            >
              <span
                :class="[
                  'status-dot',
                  check.status === 'ok' ? 'bg-green-500' :
                  check.status === 'warning' ? 'bg-amber-400' :
                  check.status === 'error' ? 'bg-red-500' : 'bg-slate-300'
                ]"
              />
              {{ check.summary }}
            </div>
          </div>

          <div class="flex items-center justify-between mt-3 pt-3 border-t border-slate-100">
            <span class="text-xs text-slate-400">
              <a
                v-if="app.host_port"
                :href="`http://${appHostname}:${app.host_port}`"
                target="_blank"
                rel="noopener noreferrer"
                class="hover:text-blue-500 hover:underline"
                @click.stop
              >
                Port {{ app.host_port }} ↗
              </a>
              <span v-else>No port</span>
            </span>
            <span
              class="text-xs"
              :class="criticalityColor(app.criticality)"
            >
              {{ app.criticality }}
            </span>
          </div>
        </RouterLink>
      </div>
    </div>

    <!-- LLM Agent status — always rendered, skeleton while loading -->
    <div class="card card-body">
      <div class="flex items-center gap-3">
        <div class="w-8 h-8 bg-violet-100 rounded-lg flex items-center justify-center text-base shrink-0">
          🤖
        </div>
        <template v-if="llmStatus">
          <div class="flex-1">
            <div class="flex items-center gap-2">
              <span class="font-medium text-sm text-slate-900">LLM Agent</span>
              <span :class="llmBadge">{{ llmStatus.status }}</span>
            </div>
            <div class="text-xs text-slate-400 mt-0.5">
              {{ llmStatus.description }}
            </div>
          </div>
          <RouterLink
            to="/models"
            class="btn-secondary btn-sm ml-auto shrink-0"
          >
            Configure
          </RouterLink>
        </template>
        <template v-else>
          <div class="flex-1 space-y-1.5 animate-pulse">
            <div class="h-3 bg-slate-100 rounded w-32" />
            <div class="h-2.5 bg-slate-100 rounded w-48" />
          </div>
          <div class="w-20 h-6 bg-slate-100 rounded animate-pulse shrink-0" />
        </template>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
const appHostname = window.location.hostname
import { appsCache, healthCache, setAppsCache, setHealthCache } from '../appCache'
import { RouterLink } from 'vue-router'
import { usePlatformStore } from '../stores/platform'
import { apps, health, catalog } from '../api/client'
import type { AppStatus, HealthCheck, CatalogEntry, AgentHealthCheck } from '../api/client'

const platformStore = usePlatformStore()
const traefikRunning = computed(() => !!platformStore.status?.traefik_version)
const loading = ref(false)
const installedApps = ref<AppStatus[]>(appsCache ?? [])
const _buildHealthMap = (checks: HealthCheck[] | null): Record<string, HealthCheck[]> => {
  if (!checks) return {}
  const g: Record<string, HealthCheck[]> = {}
  for (const c of checks) { if (!g[c.app_key]) g[c.app_key] = []; g[c.app_key].push(c) }
  return g
}
const appHealth = ref<Record<string, HealthCheck[]>>(_buildHealthMap(healthCache))
const llmStatus = ref<{ status: string; description: string } | null>(null)
const agentChecks = ref<AgentHealthCheck[]>([])
const catalogIcons = ref<Record<string, string>>({})
const unhealthyCount = computed(() =>
  Object.values(appHealth.value).flat().filter(c => c.status === 'error').length
)

const stats = computed(() => [
  {
    label: 'Installed Apps',
    value: installedApps.value.length,
    color: 'text-slate-900',
  },
  {
    label: 'Running',
    value: installedApps.value.filter(a => a.status === 'running').length,
    color: 'text-green-600',
  },
  {
    label: 'Unhealthy',
    value: unhealthyCount.value,
    color: 'text-red-500',
  },
  {
    label: 'Disabled',
    value: installedApps.value.filter(a => a.status === 'disabled').length,
    color: 'text-slate-400',
  },
])

function statusBadge(status: string) {
  const map: Record<string, string> = {
    failed:        'badge badge-red',
    misconfigured: 'badge badge-red',
    oom_killed:    'badge badge-red',
    running: 'badge-green',
    installing: 'badge-blue',
    error: 'badge-red',
    disabled: 'badge-gray',
    unhealthy: 'badge-yellow',
  }
  return `badge ${map[status] ?? 'badge-gray'}`
}

function criticalityColor(c: string) {
  const map: Record<string, string> = {
    failed:        'badge badge-red',
    misconfigured: 'badge badge-red',
    oom_killed:    'badge badge-red',
    inviolable: 'text-red-500',
    important: 'text-amber-500',
    independent: 'text-slate-400',
    enhancement: 'text-slate-300',
  }
  return map[c] ?? 'text-slate-400'
}

const llmBadge = computed(() => {
  const map: Record<string, string> = {
    failed:        'badge badge-red',
    misconfigured: 'badge badge-red',
    oom_killed:    'badge badge-red',
    active: 'badge-green',
    degraded: 'badge-yellow',
    offline: 'badge-red',
    disabled: 'badge-gray',
    unknown: 'badge-gray',
  }
  return `badge ${map[llmStatus.value?.status ?? 'unknown'] ?? 'badge-gray'}`
})

function getCatalogIcon(key: string) {
  return catalogIcons.value[key] ?? '📦'
}

function iconUrl(key: string): string {
  const name = key.replace(/_/g, '-').toLowerCase()
  return `https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/${name}.png`
}

async function refreshAll() {
  loading.value = true
  try {
    const [appList, allChecks, agentStatus, catalogData, agentHealth] = await Promise.allSettled([
      apps.list(),
      health.allApps(),
      health.llmAgent(),
      catalog.all(),
      health.agentChecks(),
    ])

    if (appList.status === 'fulfilled') {
      installedApps.value = appList.value
      setAppsCache(appList.value)
    }
    if (allChecks.status === 'fulfilled') {
      // Group by app key
      const grouped: Record<string, HealthCheck[]> = {}
      for (const check of allChecks.value) {
        if (!grouped[check.app_key]) grouped[check.app_key] = []
        grouped[check.app_key].push(check)
      }
      setHealthCache(allChecks.value)
      appHealth.value = grouped
    }
    if (agentStatus.status === 'fulfilled') llmStatus.value = agentStatus.value
    if (agentHealth.status === 'fulfilled') agentChecks.value = agentHealth.value
    if (catalogData.status === 'fulfilled') {
      for (const entries of Object.values(catalogData.value)) {
        for (const entry of entries as CatalogEntry[]) {
          catalogIcons.value[entry.key] = entry.icon
        }
      }
    }
  } finally {
    loading.value = false
  }
}

onMounted(refreshAll)
</script>
