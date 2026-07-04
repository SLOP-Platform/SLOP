<template>
  <div
    v-if="app"
    class="p-4 max-w-4xl mx-auto w-full"
  >
    <!-- Header -->
    <div class="flex items-center gap-4 mb-6">
      <RouterLink
        to="/"
        class="text-slate-400 hover:text-slate-600 text-sm"
      >
        ← Back
      </RouterLink>
      <div class="flex items-center gap-3 flex-1">
        <div class="w-10 h-10 bg-slate-100 rounded-xl flex items-center justify-center overflow-hidden">
          <img
            :src="`https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/${(app.key||'').replace(/_/g,'-')}.png`"
            :alt="app.display_name"
            class="w-8 h-8 object-contain"
            @error="(e: Event) => { const t = e.target as HTMLImageElement; if(t){t.style.display='none'; const s=t.nextElementSibling as HTMLElement; if(s) s.style.display='block'} }"
          >
          <span class="text-xl hidden">{{ icon }}</span>
        </div>
        <div>
          <h1 class="text-xl font-semibold text-slate-900">
            {{ app.display_name }}
          </h1>
          <div class="flex items-center gap-2 mt-0.5">
            <span :class="statusBadge">{{ app.status }}</span>
            <span class="text-xs text-slate-400 capitalize">{{ app.category }}</span>
          </div>
        </div>
      </div>
      <div class="flex gap-2">
        <button
          v-if="app.status === 'running'"
          class="btn-secondary btn-sm"
          @click="restart"
        >
          Restart
        </button>
        <button
          v-if="app.status === 'running'"
          class="btn-secondary btn-sm text-amber-600"
          @click="disable"
        >
          Disable
        </button>
        <button
          v-if="app.status === 'disabled'"
          class="btn-primary btn-sm"
          @click="enable"
        >
          Enable
        </button>
        <button
          :disabled="removing"
          class="btn-secondary btn-sm text-red-500"
          @click="showRemove = true"
        >
          {{ removing ? 'Removing…' : 'Remove' }}
        </button>
        <button
          v-if="app.status === 'running' || app.status === 'disabled'"
          :disabled="updating"
          class="btn-secondary btn-sm text-sky-600"
          @click="updateApp"
        >
          {{ updating ? 'Updating…' : '↑ Update' }}
        </button>
      </div>
    </div>

    <div class="grid grid-cols-1 lg:grid-cols-3 gap-4">
      <!-- Info -->
      <div class="card card-body lg:col-span-1 space-y-4">
        <div>
          <div class="section-title mb-2">
            Details
          </div>
          <dl class="space-y-2 text-sm">
            <div class="flex justify-between">
              <dt class="text-slate-500">
                Port
              </dt>
              <dd class="font-mono text-slate-900">
                {{ app.host_port ?? '—' }}
              </dd>
            </div>
            <div class="flex justify-between">
              <dt class="text-slate-500">
                Criticality
              </dt>
              <dd
                class="capitalize"
                :class="critColor"
              >
                {{ app.criticality }}
              </dd>
            </div>
            <div class="flex justify-between">
              <dt class="text-slate-500">
                Image
              </dt>
              <dd class="font-mono text-xs text-slate-600 text-right max-w-32 truncate">
                {{ app.image }}
              </dd>
            </div>
            <div class="flex justify-between items-center">
              <dt class="text-slate-500">
                Version tag
              </dt>
              <dd class="flex items-center gap-1">
                <span
                  v-if="!showVersionPin"
                  class="font-mono text-xs text-slate-600"
                >{{ (app as any).image_tag || 'latest' }}</span>
                <input
                  v-else
                  v-model="pinnedTag"
                  type="text"
                  placeholder="e.g. 4.0.9"
                  class="input text-xs w-24 py-0.5"
                >
                <button
                  class="text-xs text-sky-500 hover:text-sky-600"
                  @click="toggleVersionPin"
                >
                  {{ showVersionPin ? 'Save' : 'Pin' }}
                </button>
              </dd>
            </div>
          </dl>
        </div>
        <div v-if="app.host_port">
          <div class="section-title mb-2">
            Access
          </div>
          <a
            :href="appUrl"
            target="_blank"
            class="text-sky-500 hover:text-sky-600 text-sm font-medium"
          >
            {{ appUrl }} ↗
          </a>
        </div>
      </div>

      <!-- Health + logs -->
      <div class="lg:col-span-2 space-y-4">
        <!-- Health checks -->
        <div class="card">
          <div class="card-header flex items-center justify-between">
            <span class="font-semibold text-sm">Health Checks</span>
            <span
              v-if="healthConfig?.is_community"
              class="badge badge-yellow text-xs"
            >custom app</span>
          </div>
          <div class="card-body space-y-3">
            <!-- Catalog app or community app with no data yet (non-custom case) -->
            <div
              v-if="!health.length && !healthConfig?.is_community"
              class="text-sm text-slate-400"
            >
              No health data yet.
            </div>

            <!-- Community app: checks configured, waiting for first cycle -->
            <div
              v-if="healthConfig?.is_community && (healthConfig.checks_defined ?? 0) > 0 && !health.length"
              class="flex items-center gap-2 text-sm text-slate-400"
            >
              <span class="inline-block w-2 h-2 rounded-full bg-sky-400 animate-pulse" />
              Waiting for first health check (~60s)…
            </div>

            <!-- Community app: no checks — show configure panel -->
            <div
              v-if="healthConfig?.is_community && (healthConfig.checks_defined ?? 0) === 0"
              class="space-y-3"
            >
              <p class="text-xs bg-amber-50 text-amber-700 rounded-lg px-3 py-2">
                ⚠ No health monitoring configured for this custom app.
              </p>
              <template v-if="app?.host_port">
                <div class="flex gap-2 items-center">
                  <label class="text-xs text-slate-500 w-20 shrink-0">Health path</label>
                  <input
                    v-model="enhanceForm.health_path"
                    type="text"
                    class="input text-xs flex-1"
                    placeholder="/health"
                  >
                  <button
                    :disabled="detectingHealth"
                    class="btn-secondary btn-sm text-xs shrink-0"
                    @click="autoDetectHealth"
                  >
                    {{ detectingHealth ? '…' : 'Auto-detect' }}
                  </button>
                </div>
                <div
                  v-if="enhanceResult"
                  :class="[
                    'text-xs rounded px-2 py-1.5',
                    enhanceResult.ok ? 'bg-green-50 text-green-700' : 'bg-amber-50 text-amber-700'
                  ]"
                >
                  {{ enhanceResult.message }}
                </div>
                <button
                  :disabled="savingEnhancement"
                  class="btn-primary btn-sm text-xs"
                  @click="saveEnhancement"
                >
                  {{ savingEnhancement ? 'Saving…' : 'Enable health monitoring' }}
                </button>
              </template>
              <p
                v-else
                class="text-xs text-slate-400"
              >
                App has no mapped host port — health checks require a port binding.
                Reinstall with a host port to enable monitoring.
              </p>
            </div>

            <!-- Health check results (catalog and community alike) -->
            <div
              v-for="check in health"
              :key="check.check_name"
              class="flex items-center justify-between py-2 border-b border-slate-50 last:border-0"
            >
              <div class="flex items-center gap-2">
                <span
                  :class="[
                    'status-dot',
                    check.status === 'ok' ? 'bg-green-500' :
                    check.status === 'warning' ? 'bg-amber-400' :
                    check.status === 'error' ? 'bg-red-500' : 'bg-slate-300'
                  ]"
                />
                <span class="text-sm font-medium text-slate-700">{{ check.check_name }}</span>
              </div>
              <span class="text-sm text-slate-500">{{ check.summary }}</span>
            </div>
          </div>
        </div>


        <!-- Post-install guidance -->
        <div
          v-if="postInstallSteps.length"
          class="card"
        >
          <div class="card-header flex items-center justify-between">
            <span class="font-semibold text-sm">Setup checklist</span>
            <span class="text-xs text-slate-400">{{ completedSteps }}/{{ postInstallSteps.length }} done</span>
          </div>
          <div class="card-body space-y-2">
            <div
              v-for="(step, idx) in postInstallSteps"
              :key="idx"
              :class="['rounded-lg border px-3 py-2.5 transition-colors',
                       step._done ? 'border-green-100 bg-green-50' : 'border-slate-200']"
            >
              <div class="flex items-start gap-2">
                <button
                  :class="['mt-0.5 w-4 h-4 rounded border flex items-center justify-center shrink-0 transition-colors',
                           step._done ? 'bg-green-500 border-green-500 text-white' : 'border-slate-300']"
                  @click="step._done = !step._done"
                >
                  <span
                    v-if="step._done"
                    class="text-xs leading-none"
                  >✓</span>
                </button>
                <div class="flex-1 min-w-0">
                  <div :class="['text-sm font-medium', step._done ? 'line-through text-slate-400' : 'text-slate-800']">
                    {{ step.title }}
                    <span
                      v-if="step.required"
                      class="ml-1 text-xs text-red-400"
                    >required</span>
                  </div>
                  <div class="text-xs text-slate-500 mt-0.5">
                    {{ step.description }}
                  </div>
                </div>
                <a
                  v-if="step.link"
                  :href="step.link"
                  target="_blank"
                  rel="noopener"
                  class="text-xs text-sky-500 hover:text-sky-600 shrink-0"
                >Open ↗</a>
              </div>
            </div>
          </div>
        </div>

        <!-- Logs -->
        <div class="card">
          <div class="card-header flex items-center justify-between">
            <span class="font-semibold text-sm">Container Logs</span>
            <button
              class="text-xs text-sky-500 hover:text-sky-600"
              @click="fetchLogs"
            >
              Refresh
            </button>
          </div>
          <div class="bg-slate-950 rounded-b-xl px-4 py-3 max-h-64 overflow-y-auto">
            <pre class="text-xs text-slate-300 font-mono whitespace-pre-wrap">{{ logs || 'No logs available.' }}</pre>
          </div>
        </div>
      </div>
    </div>


    <!-- App Configuration (config_schema driven) -->
    <div
      v-if="appConfig && appConfig.schema && appConfig.schema.length"
      class="card"
    >
      <div class="card-header flex items-center justify-between">
        <span class="font-semibold text-sm">Configuration</span>
        <span
          v-if="configSaved"
          class="text-xs text-green-600 font-medium"
        >Saved ✓</span>
      </div>
      <div class="card-body space-y-4">
        <div
          v-for="field in appConfig.schema"
          :key="field.key"
        >
          <!-- Providers list (DDNS Updater style) -->
          <template v-if="field.type === 'providers_list'">
            <label class="label">{{ field.label }}</label>
            <div class="space-y-2">
              <div
                v-for="(provider, idx) in (configValues.providers || [])"
                :key="idx"
                class="flex gap-2 items-center rounded-lg border border-slate-200 px-3 py-2"
              >
                <input
                  v-model="provider.provider"
                  type="text"
                  placeholder="cloudflare"
                  class="input text-xs flex-1"
                  @input="configDirty = true"
                >
                <input
                  v-model="provider.host"
                  type="text"
                  placeholder="@ or subdomain"
                  class="input text-xs w-28"
                  @input="configDirty = true"
                >
                <input
                  v-model="provider.token"
                  type="password"
                  placeholder="API token"
                  class="input text-xs w-36"
                  @input="configDirty = true"
                >
                <button
                  class="text-slate-300 hover:text-red-400 shrink-0"
                  @click="removeProvider(idx)"
                >
                  ✕
                </button>
              </div>
              <button
                class="text-xs text-sky-500 hover:text-sky-600 font-medium"
                @click="addProvider"
              >
                + Add provider
              </button>
            </div>
            <p class="text-xs text-slate-400 mt-1">
              {{ field.help }}
            </p>
          </template>
          <!-- Select -->
          <template v-else-if="field.type === 'select'">
            <label class="label">{{ field.label }}</label>
            <select
              v-model="configValues[field.key]"
              class="input"
              @change="configDirty = true"
            >
              <option
                v-for="opt in field.options"
                :key="opt"
                :value="opt"
              >
                {{ opt }}
              </option>
            </select>
            <p
              v-if="field.help"
              class="text-xs text-slate-400 mt-1"
            >
              {{ field.help }}
            </p>
          </template>
          <!-- Default text input -->
          <template v-else>
            <label class="label">{{ field.label }}
              <span
                v-if="field.required"
                class="text-red-400 ml-0.5"
              >*</span>
            </label>
            <input
              v-model="configValues[field.key]"
              :type="field.secret ? 'password' : 'text'"
              :placeholder="field.placeholder || ''"
              class="input"
              @input="configDirty = true"
            >
            <p
              v-if="field.help"
              class="text-xs text-slate-400 mt-1"
            >
              {{ field.help }}
            </p>
          </template>
        </div>

        <div class="flex gap-2 pt-2 border-t border-slate-100">
          <button
            :disabled="!configDirty || savingConfig"
            class="btn-primary btn-sm"
            @click="saveConfig"
          >
            {{ savingConfig ? 'Saving…' : 'Save configuration' }}
          </button>
          <button
            :disabled="!configSaved"
            class="btn-secondary btn-sm"
            @click="applyConfig"
          >
            Apply & restart
          </button>
        </div>
      </div>
    </div>

    <!-- Remove modal -->
    <Teleport to="body">
      <div
        v-if="showRemove"
        class="fixed inset-0 z-50 flex items-center justify-center"
      >
        <div
          class="absolute inset-0 bg-black/30 backdrop-blur-sm"
          @click="showRemove = false"
        />
        <div class="relative card w-full max-w-sm mx-4 card-body">
          <h3 class="font-semibold text-slate-900">
            Remove {{ app.display_name }}?
          </h3>
          <p class="text-sm text-slate-500 mt-2">
            The container will be stopped and removed. Wiring to other apps will be cleaned up.
          </p>
          <label class="flex items-center gap-2 mt-3 cursor-pointer">
            <input
              v-model="deleteConfig"
              type="checkbox"
              class="rounded border-slate-300"
            >
            <span class="text-sm text-slate-700">Also delete config folder</span>
          </label>
          <div class="flex gap-3 mt-4">
            <button
              class="btn-secondary flex-1"
              @click="showRemove = false"
            >
              Cancel
            </button>
            <button
              class="btn-danger flex-1"
              @click="doRemove"
            >
              Remove
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
  <div
    v-else
    class="p-6 text-center text-slate-400"
  >
    Loading…
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { RouterLink, useRoute, useRouter } from 'vue-router'
import { usePlatformStore } from '../stores/platform'
import { useToast } from '@/composables/useToast'
const toast = useToast()
import { apps as appsApi, health as healthApi, catalog } from '../api/client'
import type { AppStatus, HealthCheck, CatalogEntry } from '../api/client'

const route = useRoute()
const router = useRouter()
const key = route.params.key as string

const app = ref<AppStatus | null>(null)
const health = ref<HealthCheck[]>([])
const logs = ref('')
const icon = ref('📦')
const showRemove = ref(false)
const updating = ref(false)
const showVersionPin = ref(false)
const pinnedTag = ref('')
const postInstallSteps = ref<any[]>([])
const deleteConfig = ref(false)
const appConfig = ref<any>(null)
const configValues = ref<Record<string, any>>({})
const configDirty = ref(false)
const savingConfig = ref(false)
const configSaved = ref(false)

const healthConfig = ref<{
  key: string
  has_manifest: boolean
  is_community: boolean
  checks_defined: number
  checks: Array<{ name: string; type: string; path: string; expect_status: number; interval: number }>
  current_status: Array<{ check_name: string; status: string; summary: string }>
} | null>(null)

const completedSteps = computed(() => postInstallSteps.value.filter(s => s._done).length)

const statusBadge = computed(() => {
  const map: Record<string, string> = {
    running: 'badge-green', installing: 'badge-blue',
    error: 'badge-red', disabled: 'badge-gray', unhealthy: 'badge-yellow'
  }
  return `badge ${map[app.value?.status ?? ''] ?? 'badge-gray'}`
})


const appUrl = computed(() => {
  if (!app.value) return '#'
  const store = usePlatformStore()
  const domain = store.domain
  if (domain) return `https://${key}.${domain}`
  return app.value.host_port ? `http://localhost:${app.value.host_port}` : '#'
})

const critColor = computed(() => ({
  inviolable: 'text-red-500',
  important: 'text-amber-500',
  independent: 'text-slate-600',
  enhancement: 'text-slate-400',
}[app.value?.criticality ?? ''] ?? 'text-slate-400'))

async function fetchLogs() {
  try { logs.value = (await appsApi.logs(key)).logs } catch { logs.value = 'Could not retrieve logs.' }
}

async function restart() {
  try { await appsApi.restart(key); app.value = await appsApi.get(key); toast.success(`${app.value?.display_name ?? key} restarted.`) }
  catch (e) { toast.error('Restart failed.', e instanceof Error ? e.message : String(e)) }
}

async function disable() {
  try { await appsApi.disable(key); app.value = await appsApi.get(key); toast.warn(`${app.value?.display_name ?? key} disabled.`) }
  catch (e) { toast.error('Could not disable.', e instanceof Error ? e.message : String(e)) }
}

async function enable() {
  try { await appsApi.enable(key); app.value = await appsApi.get(key); toast.success(`${app.value?.display_name ?? key} enabled.`) }
  catch (e) { toast.error('Could not enable.', e instanceof Error ? e.message : String(e)) }
}

async function updateApp() {
  updating.value = true
  try {
    const r = await fetch(`/api/v1/apps/${key}/update`, { method: 'POST' })
    const data = await r.json()
    if (data.ok) {
      toast.success('Update started.', 'Check progress below.')
    } else {
      toast.error('Update failed.', data.detail ?? '')
    }
  } catch (e) {
    toast.error('Update failed.', String(e))
  } finally {
    updating.value = false
  }
}

async function toggleVersionPin() {
  if (showVersionPin.value && pinnedTag.value) {
    // Save the pinned tag
    try {
      await fetch(`/api/v1/apps/${key}/pin-version`, {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ image_tag: pinnedTag.value }),
      })
      toast.success(`Tag pinned to ${pinnedTag.value}`)
      if (app.value) (app.value as any).image_tag = pinnedTag.value
    } catch (e) {
      toast.error('Could not pin tag.', String(e))
    }
  } else {
    pinnedTag.value = (app.value as any)?.image_tag || 'latest'
  }
  showVersionPin.value = !showVersionPin.value
}

async function loadPostInstallSteps() {
  try {
    const r = await fetch(`/api/v1/apps/${key}/post-install-steps`)
    if (r.ok) {
      const steps = await r.json()
      postInstallSteps.value = steps.map((s: any) => ({ ...s, _done: false }))
    }
  } catch { /* intentional: post-install steps missing is non-fatal */ }
}

async function loadHealthConfig() {
  try {
    const r = await fetch(`/api/v1/apps/${key}/health-config`)
    if (r.ok) healthConfig.value = await r.json()
  } catch { /* intentional: health config missing is non-fatal */ }
}

async function loadConfig() {
  try {
    const r = await fetch(`/api/v1/apps/${key}/config`)
    if (r.ok) {
      appConfig.value = await r.json()
      configValues.value = { ...appConfig.value.values }
    }
  } catch { /* intentional: config load failure is non-fatal */ }
}

async function saveConfig() {
  savingConfig.value = true
  try {
    const r = await fetch(`/api/v1/apps/${key}/config`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ values: configValues.value }),
    })
    if (r.ok) {
      configDirty.value = false
      configSaved.value = true
      toast.success('Configuration saved.')
      setTimeout(() => configSaved.value = false, 3000)
    } else {
      toast.error('Could not save config.')
    }
  } catch (e) {
    toast.error('Save failed.', String(e))
  } finally {
    savingConfig.value = false
  }
}

async function applyConfig() {
  await restart()
  toast.info('Container restarted with new configuration.')
}

function addProvider() {
  if (!configValues.value.providers) configValues.value.providers = []
  configValues.value.providers.push({ provider: '', host: '@', token: '' })
  configDirty.value = true
}

function removeProvider(idx: number) {
  configValues.value.providers?.splice(idx, 1)
  configDirty.value = true
}

const removing = ref(false)

async function doRemove() {
  showRemove.value = false
  removing.value = true
  const name = app.value?.display_name ?? key
  try {
    await appsApi.remove(key, deleteConfig.value)
    toast.success(`${name} removed.`)
    router.push('/')
  } catch (e) {
    removing.value = false
    toast.error(`Could not remove ${name}.`, e instanceof Error ? e.message : String(e))
  }
}

async function loadApp() {
  // Reload app data after changes
  try {
    const res = await fetch(`/api/v1/apps/${route.params.key}`)
    app.value = await res.json()
    pinnedTag.value = (app.value as any).pinned_tag || ''
  } catch { /* intentional: reload failure is non-fatal */ }
}


const enhanceForm = ref({
  health_path: '/health',
  start_grace_s: 60,
  category: 'tools',
  display_name: '',
})
const savingEnhancement = ref(false)
const detectingHealth = ref(false)
const enhanceResult = ref<{ ok: boolean; message: string } | null>(null)

async function autoDetectHealth() {
  if (!app.value?.host_port) return
  detectingHealth.value = true
  const paths = ['/health', '/api/ping', '/api/v1/ping', '/ping', '/healthz', '/']
  for (const path of paths) {
    try {
      const r = await fetch(`/api/v1/apps/${app.value.key}/probe-path`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ path }),
      })
      if (r.ok) {
        const d = await r.json()
        if (d.reachable) {
          enhanceForm.value.health_path = path
          enhanceResult.value = { ok: true, message: `Auto-detected: ${path} returns HTTP ${d.status}` }
          detectingHealth.value = false
          return
        }
      }
    } catch { /* intentional: probe path failure skips to next candidate */ }
  }
  enhanceResult.value = { ok: false, message: 'Could not auto-detect path — enter it manually.' }
  detectingHealth.value = false
}

async function saveEnhancement() {
  if (!app.value) return
  savingEnhancement.value = true
  enhanceResult.value = null
  try {
    const r = await fetch(`/api/v1/apps/${app.value.key}/enhance`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(enhanceForm.value),
    })
    const d = await r.json()
    enhanceResult.value = { ok: r.ok, message: d.message || (r.ok ? 'Monitoring enhanced.' : 'Failed.') }
    if (r.ok) {
      await loadApp()
      await loadHealthConfig()
    }
  } catch (e) {
    enhanceResult.value = { ok: false, message: String(e) }
  } finally {
    savingEnhancement.value = false
  }
}

onMounted(async () => {
  const [appData, healthData, catalogData] = await Promise.allSettled([
    appsApi.get(key),
    healthApi.app(key),
    catalog.all(),
  ])
  if (appData.status === 'fulfilled') {
    app.value = appData.value
    enhanceForm.value.display_name = appData.value.display_name
    enhanceForm.value.category = appData.value.category || 'tools'
  }
  if (healthData.status === 'fulfilled') health.value = healthData.value
  if (catalogData.status === 'fulfilled') {
    for (const entries of Object.values(catalogData.value)) {
      const found = (entries as CatalogEntry[]).find(e => e.key === key)
      if (found) { icon.value = found.icon; break }
    }
  }
  await fetchLogs()
  await loadConfig()
  await loadPostInstallSteps()
  await loadHealthConfig()
})
</script>