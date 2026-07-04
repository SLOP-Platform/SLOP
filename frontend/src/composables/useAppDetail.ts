import { ref, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { usePlatformStore } from '../stores/platform'
import { useToast } from '@/composables/useToast'
import { apps as appsApi, health as healthApi, catalog } from '../api/client'
import type { AppStatus, HealthCheck, CatalogEntry } from '../api/client'

interface HealthConfig {
  key: string
  has_manifest: boolean
  is_community: boolean
  checks_defined: number
  checks: Array<{ name: string; type: string; path: string; expect_status: number; interval: number }>
  current_status: Array<{ check_name: string; status: string; summary: string }>
}

/**
 * All AppDetailView business logic — state, derived values, and actions.
 * Extracted from the view to satisfy the "no business logic in views" rule
 * (rule-007 / frontend architecture rule in CLAUDE.md). The view keeps only
 * template markup wired to the refs/handlers returned here.
 */
export function useAppDetail() {
  const route = useRoute()
  const router = useRouter()
  const toast = useToast()
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
  const removing = ref(false)

  const healthConfig = ref<HealthConfig | null>(null)

  const enhanceForm = ref({
    health_path: '/health',
    start_grace_s: 60,
    category: 'tools',
    display_name: '',
  })
  const savingEnhancement = ref(false)
  const detectingHealth = ref(false)
  const enhanceResult = ref<{ ok: boolean; message: string } | null>(null)

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
      const { data } = await appsApi.update(key)
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
        await appsApi.pinVersion(key, pinnedTag.value)
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
      const steps = await appsApi.postInstallSteps(key)
      postInstallSteps.value = steps.map((s: any) => ({ ...s, _done: false }))
    } catch { /* intentional: post-install steps missing is non-fatal */ }
  }

  async function loadHealthConfig() {
    try {
      healthConfig.value = await appsApi.healthConfig(key)
    } catch { /* intentional: health config missing is non-fatal */ }
  }

  async function loadConfig() {
    try {
      appConfig.value = await appsApi.config(key)
      configValues.value = { ...appConfig.value.values }
    } catch { /* intentional: config load failure is non-fatal */ }
  }

  async function saveConfig() {
    savingConfig.value = true
    try {
      const r = await appsApi.saveConfig(key, configValues.value)
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
      app.value = await appsApi.get(String(route.params.key))
      pinnedTag.value = (app.value as any).pinned_tag || ''
    } catch { /* intentional: reload failure is non-fatal */ }
  }

  async function autoDetectHealth() {
    if (!app.value?.host_port) return
    detectingHealth.value = true
    const paths = ['/health', '/api/ping', '/api/v1/ping', '/ping', '/healthz', '/']
    for (const path of paths) {
      try {
        const d = await appsApi.probePath(app.value.key, path)
        if (d.reachable) {
          enhanceForm.value.health_path = path
          enhanceResult.value = { ok: true, message: `Auto-detected: ${path} returns HTTP ${d.status}` }
          detectingHealth.value = false
          return
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
      const { ok, data: d } = await appsApi.enhance(app.value.key, enhanceForm.value)
      enhanceResult.value = { ok, message: d.message || (ok ? 'Monitoring enhanced.' : 'Failed.') }
      if (ok) {
        await loadApp()
        await loadHealthConfig()
      }
    } catch (e) {
      enhanceResult.value = { ok: false, message: String(e) }
    } finally {
      savingEnhancement.value = false
    }
  }

  async function init() {
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
  }

  return {
    // state
    app, health, logs, icon, showRemove, updating, showVersionPin, pinnedTag,
    postInstallSteps, deleteConfig, appConfig, configValues, configDirty,
    savingConfig, configSaved, removing, healthConfig, enhanceForm,
    savingEnhancement, detectingHealth, enhanceResult,
    // derived
    completedSteps, statusBadge, appUrl, critColor,
    // actions
    fetchLogs, restart, disable, enable, updateApp, toggleVersionPin,
    saveConfig, applyConfig, addProvider, removeProvider, doRemove,
    autoDetectHealth, saveEnhancement, init,
  }
}
