<template>
  <div class="p-4 max-w-4xl mx-auto w-full">
    <div class="flex items-center justify-between mb-6">
      <div>
        <h1 class="page-title">
          Routing
        </h1>
        <p class="page-subtitle">
          Configure debrid vs. download paths per media type
        </p>
      </div>
    </div>

    <!-- How it works — slide-in panel to right of main column -->
    <Teleport to="body">
      <div
        v-if="showHowItWorks"
        class="fixed inset-0 z-40"
        @click.self="showHowItWorks = false"
      >
        <div class="absolute right-0 top-0 h-full w-80 bg-white border-l border-slate-200 shadow-xl flex flex-col">
          <div class="flex items-center justify-between px-5 py-4 border-b border-slate-100">
            <span class="font-semibold text-slate-900 text-sm">How routing works</span>
            <button
              class="text-slate-400 hover:text-slate-600"
              @click="showHowItWorks = false"
            >
              ✕
            </button>
          </div>
          <div class="flex-1 overflow-y-auto px-5 py-4 space-y-4 text-sm text-slate-600">
            <p><strong class="text-slate-900">Dual-path routing</strong> lets different users get content from different sources.</p>
            <p>For each media type you run two arr instances — one pointing at a debrid provider (Decypharr/DUMB) and one at a traditional downloader (qBittorrent/SABnzbd).</p>
            <p>Seerr assigns users to paths. Premium users get ⚡ <strong>Debrid</strong> (instant), others get ⬇ <strong>Download</strong> (queued).</p>
            <div class="rounded-lg bg-slate-50 border border-slate-200 p-3 space-y-2 text-xs font-mono text-slate-500">
              <div>Seerr → Sonarr (debrid) → Decypharr → ⚡</div>
              <div>Seerr → Sonarr (dl) → qBittorrent → 💾</div>
            </div>
          </div>
        </div>
      </div>
    </Teleport>

    <!-- Installed arr instances across all types -->
    <div class="mb-6">
      <h2 class="font-semibold text-slate-900 mb-3">
        Installed Arr Instances
      </h2>
      <div
        v-if="!allInstances.length"
        class="card card-body text-sm text-slate-400 text-center py-6"
      >
        No multi-instance routing configured yet. Deploy a debrid instance below to get started.
      </div>
      <div
        v-else
        class="grid gap-2 sm:grid-cols-2 lg:grid-cols-3"
      >
        <div
          v-for="inst in allInstances"
          :key="inst.instance_key"
          class="card card-body flex items-center gap-3"
        >
          <div class="w-9 h-9 bg-slate-100 rounded-xl flex items-center justify-center text-base shrink-0">
            {{ roleIcon(inst.role) }}
          </div>
          <div class="flex-1 min-w-0">
            <div class="text-sm font-medium text-slate-900 truncate">
              {{ inst.label }}
            </div>
            <div class="flex items-center gap-1.5 mt-0.5 flex-wrap">
              <span
                class="badge text-xs"
                :class="roleBadge(inst.role)"
              >{{ inst.role }}</span>
              <span class="text-xs text-slate-400">{{ inst.manifest_key }}</span>
              <span
                v-if="inst.host_port"
                class="text-xs text-slate-400"
              >:{{ inst.host_port }}</span>
            </div>
          </div>
          <button
            class="text-slate-300 hover:text-red-400 shrink-0"
            @click="confirmRemoveInstance(inst)"
          >
            ✕
          </button>
        </div>
      </div>
    </div>

    <!-- Per-media-type routing cards -->
    <h2 class="font-semibold text-slate-900 mb-3">
      Media Type Configuration
    </h2>
    <div class="space-y-3">
      <div
        v-for="route in routes"
        :key="route.media_type"
        class="card overflow-hidden"
      >
        <!-- Card header row -->
        <div
          class="card-body !py-2.5 flex items-center justify-between cursor-pointer select-none"
          @click="toggleExpand(route.media_type)"
        >
          <div class="flex items-center gap-3">
            <div class="w-8 h-8 flex items-center justify-center overflow-hidden shrink-0">
              <img
                :src="mediaTypeIconUrl(route.media_type)"
                class="w-7 h-7 object-contain"
                @error="(e: Event) => { const t = e.target as HTMLImageElement; if(t){t.style.display='none'; const s=t.nextElementSibling as HTMLElement; if(s) s.style.display='block'} }"
              >
              <span class="text-xl hidden">{{ TYPE_ICONS[route.media_type] ?? '📁' }}</span>
            </div>
            <div>
              <div class="font-medium text-slate-900 capitalize">
                {{ route.media_type }}
              </div>
              <div class="text-xs text-slate-400">
                {{ route.canonical_manifest }}
              </div>
            </div>
          </div>
          <div class="flex items-center gap-3">
            <!-- Seerr badge -->
            <span
              v-if="route.seerr_supported"
              class="badge badge-blue text-xs"
            >Seerr ✓</span>
            <span
              v-else
              class="badge badge-gray text-xs"
            >Seerr n/a</span>

            <!-- Routing status -->
            <div class="text-xs text-right hidden sm:block">
              <div
                v-if="route.debrid_instance"
                class="text-sky-600"
              >
                ⚡ {{ route.debrid_instance }}
              </div>
              <div
                v-if="route.download_instance"
                class="text-slate-500"
              >
                ⬇ {{ route.download_instance }}
              </div>
              <div
                v-if="!route.debrid_instance && !route.download_instance"
                class="text-slate-300"
              >
                not configured
              </div>
            </div>

            <!-- Default path badge -->
            <span :class="['badge text-xs', route.default_path === 'debrid' ? 'badge-blue' : route.default_path === 'ask' ? 'badge-yellow' : 'badge-gray']">
              {{ route.default_path }}
            </span>
            <span class="text-slate-300 text-xs">{{ expanded === route.media_type ? '▲' : '▼' }}</span>
          </div>
        </div>

        <!-- Expanded panel -->
        <div
          v-if="expanded === route.media_type"
          class="border-t border-slate-100 px-4 py-3 space-y-3"
        >
          <!-- Instance assignment -->
          <div class="grid sm:grid-cols-2 gap-3">
            <div>
              <label class="label">⚡ Debrid instance</label>
              <div class="flex gap-2">
                <select
                  v-model="pendingRoutes[route.media_type].debrid_instance"
                  class="input flex-1"
                >
                  <option value="">
                    None
                  </option>
                  <option
                    v-for="inst in debridInstances(route.canonical_manifest)"
                    :key="inst.instance_key"
                    :value="inst.instance_key"
                  >
                    {{ inst.label }}
                  </option>
                </select>
              </div>
            </div>
            <div>
              <label class="label">⬇ Download instance</label>
              <select
                v-model="pendingRoutes[route.media_type].download_instance"
                class="input"
              >
                <option value="">
                  None
                </option>
                <option
                  v-for="inst in downloadInstances(route.canonical_manifest)"
                  :key="inst.instance_key"
                  :value="inst.instance_key"
                >
                  {{ inst.label }}
                </option>
              </select>
            </div>
          </div>

          <!-- Default path -->
          <div>
            <label class="label">Default path</label>
            <div class="flex gap-2">
              <label
                v-for="opt in ['debrid','download','ask']"
                :key="opt"
                :class="['flex-1 text-center py-2 rounded-lg border text-sm cursor-pointer transition-colors capitalize',
                         pendingRoutes[route.media_type].default_path === opt
                           ? 'border-sky-400 bg-sky-50 text-sky-700 font-medium'
                           : 'border-slate-200 hover:border-slate-300 text-slate-600']"
              >
                <input
                  v-model="pendingRoutes[route.media_type].default_path"
                  type="radio"
                  :value="opt"
                  class="sr-only"
                >
                {{ opt === 'debrid' ? '⚡ Debrid' : opt === 'ask' ? '❓ Ask user' : '⬇ Download' }}
              </label>
            </div>
          </div>

          <!-- Save routing -->
          <div class="flex items-center gap-3">
            <button
              :disabled="savingRoute === route.media_type"
              class="btn-primary btn-sm"
              @click="saveRouting(route.media_type)"
            >
              {{ savingRoute === route.media_type ? 'Saving…' : 'Save routing' }}
            </button>
            <span
              v-if="savedRoute === route.media_type"
              class="text-sm text-green-600"
            >✓ Saved</span>
          </div>

          <!-- Seerr help -->
          <div
            v-if="route.seerr_supported"
            class="border-t border-slate-100 pt-4"
          >
            <div class="flex items-center justify-between mb-2">
              <span class="section-title">Seerr configuration</span>
              <button
                class="text-xs text-sky-500 hover:text-sky-600"
                @click="loadSeerrHelp(route.media_type)"
              >
                Load steps →
              </button>
            </div>
            <div
              v-if="seerrHelp[route.media_type]"
              class="space-y-2 text-sm text-slate-700"
            >
              <div v-if="seerrHelp[route.media_type].seerr_steps">
                <div
                  v-for="(step, i) in seerrHelp[route.media_type].seerr_steps"
                  :key="i"
                  class="flex items-start gap-2"
                >
                  <span class="font-mono text-xs text-slate-400 mt-0.5 shrink-0">{{ String(i+1).padStart(2,'0') }}</span>
                  <span>{{ step }}</span>
                </div>
              </div>
              <div
                v-if="seerrHelp[route.media_type].arr_steps"
                class="mt-3 pt-3 border-t border-slate-100"
              >
                <div class="text-xs font-medium text-slate-500 mb-2">
                  Arr download client setup
                </div>
                <div
                  v-for="(step, i) in seerrHelp[route.media_type].arr_steps"
                  :key="i"
                  class="flex items-start gap-2 text-slate-600"
                >
                  <span class="font-mono text-xs text-slate-400 mt-0.5 shrink-0">{{ String(i+1).padStart(2,'0') }}</span>
                  <span>{{ step }}</span>
                </div>
              </div>
            </div>
          </div>

          <!-- Deploy new instance -->
          <div class="border-t border-slate-100 pt-4">
            <div class="flex items-center justify-between mb-3">
              <span class="section-title">Deploy new instance</span>
            </div>
            <div class="grid sm:grid-cols-3 gap-3">
              <div>
                <label class="label">Role</label>
                <select
                  v-model="newInst[route.media_type].role"
                  class="input"
                >
                  <option value="debrid">
                    ⚡ Debrid
                  </option>
                  <option value="download">
                    ⬇ Download
                  </option>
                  <option value="secondary">
                    Secondary
                  </option>
                </select>
              </div>
              <div>
                <label class="label">Label</label>
                <input
                  v-model="newInst[route.media_type].label"
                  class="input"
                  :placeholder="`${route.canonical_manifest} (${newInst[route.media_type].role})`"
                >
              </div>
              <div>
                <label class="label">Port override <span class="text-slate-400 font-normal">(opt)</span></label>
                <input
                  v-model.number="newInst[route.media_type].host_port"
                  type="number"
                  class="input"
                  placeholder="auto"
                >
              </div>
            </div>
            <div
              v-if="deployErrors[route.media_type]"
              class="mt-2 text-sm text-red-600 bg-red-50 rounded p-2"
            >
              {{ deployErrors[route.media_type] }}
            </div>
            <button
              :disabled="deployingInst === route.media_type"
              class="btn-primary btn-sm mt-3"
              @click="deployInstance(route)"
            >
              {{ deployingInst === route.media_type ? 'Deploying…' : `Deploy ${route.canonical_manifest} instance` }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- Remove instance confirm -->
    <Teleport to="body">
      <div
        v-if="removeTarget"
        class="fixed inset-0 z-50 flex items-center justify-center"
      >
        <div
          class="absolute inset-0 bg-black/30 backdrop-blur-sm"
          @click="removeTarget = null"
        />
        <div class="relative card w-full max-w-sm mx-4 card-body">
          <h3 class="font-semibold text-slate-900">
            Remove {{ removeTarget.label }}?
          </h3>
          <p class="text-sm text-slate-500 mt-1">
            The container will be stopped. Any routing entries pointing to this instance will be cleared.
          </p>
          <div class="flex gap-3 mt-4">
            <button
              class="btn-secondary flex-1"
              @click="removeTarget = null"
            >
              Cancel
            </button>
            <button
              class="btn-danger flex-1"
              @click="doRemoveInstance"
            >
              Remove
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'

import { routing as routingApi } from '../api/client'
import type { RoutingConfig } from '../api/client'

function mediaTypeIconUrl(type: string): string {
  const map: Record<string, string> = {
    movies: 'radarr', tv: 'sonarr', music: 'lidarr',
    books: 'readarr', comics: 'mylar3', audiobooks: 'audiobookshelf',
  }
  const name = map[type] || type
  return `https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/${name}.png`
}

const TYPE_ICONS: Record<string, string> = {
  movies: '🎬', tv: '📺', music: '🎵', books: '📗',
  comics: '🦸', audiobooks: '🎧', adult: '🔞',
}

const routes = ref<RoutingConfig[]>([])
const allInstances = ref<any[]>([])
const expanded = ref<string | null>(null)
const showHowItWorks = ref(false)
const savingRoute = ref<string | null>(null)
const savedRoute = ref<string | null>(null)
const deployingInst = ref<string | null>(null)
const seerrHelp = reactive<Record<string, any>>({})
const deployErrors = reactive<Record<string, string | null>>({})
const removeTarget = ref<any>(null)

// Per-type: pending routing changes
const pendingRoutes = reactive<Record<string, {
  debrid_instance: string
  download_instance: string
  default_path: string
}>>({})

// Per-type: new instance form
const newInst = reactive<Record<string, {
  role: string; label: string; host_port: number | null
}>>({})

function roleIcon(role: string) {
  return { debrid: '⚡', download: '⬇', default: '📦', secondary: '📦' }[role] ?? '📦'
}
function roleBadge(role: string) {
  return { debrid: 'badge-blue', download: 'badge-gray', secondary: 'badge-gray' }[role] ?? 'badge-gray'
}

function debridInstances(manifest: string) {
  return allInstances.value.filter(i =>
    i.manifest_key === manifest && i.role === 'debrid'
  )
}
function downloadInstances(manifest: string) {
  return allInstances.value.filter(i =>
    i.manifest_key === manifest && ['download', 'default'].includes(i.role)
  )
}

function toggleExpand(type: string) {
  expanded.value = expanded.value === type ? null : type
}

async function saveRouting(type: string) {
  savingRoute.value = type
  try {
    const p = pendingRoutes[type]
    await routingApi.updateMedia(type, {
      debrid_instance: p.debrid_instance || null,
      download_instance: p.download_instance || null,
      default_path: p.default_path,
    })
    savedRoute.value = type
    setTimeout(() => { savedRoute.value = null }, 2500)
    await loadRoutes()
  } catch { /* intentional: route save failure handled by toast before throw */ } finally {
    savingRoute.value = null
  }
}

async function loadSeerrHelp(type: string) {
  try {
    seerrHelp[type] = await routingApi.seerrHelp(type)
  } catch { /* intentional: seerr help missing is non-fatal */ }
}

async function deployInstance(route: RoutingConfig) {
  deployingInst.value = route.media_type
  deployErrors[route.media_type] = null
  const form = newInst[route.media_type]
  const manifest = route.canonical_manifest
  const instanceKey = `${manifest}_${form.role}_${Date.now()}`
  try {
    await routingApi.installInstance(manifest, {
      instance_key: instanceKey,
      label: form.label || `${manifest} (${form.role})`,
      role: form.role,
      host_port: form.host_port || undefined,
    })
    await loadInstances()
    // Reset form
    newInst[route.media_type] = { role: 'debrid', label: '', host_port: null }
  } catch (e) {
    deployErrors[route.media_type] = e instanceof Error ? e.message : String(e)
  } finally {
    deployingInst.value = null
  }
}

function confirmRemoveInstance(inst: any) {
  removeTarget.value = inst
}

async function doRemoveInstance() {
  if (!removeTarget.value) return
  try {
    await routingApi.removeInstance(removeTarget.value.instance_key)
    await loadInstances()
    await loadRoutes()
  } finally {
    removeTarget.value = null
  }
}

function initPending(r: RoutingConfig) {
  if (!pendingRoutes[r.media_type]) {
    pendingRoutes[r.media_type] = {
      debrid_instance: r.debrid_instance ?? '',
      download_instance: r.download_instance ?? '',
      default_path: r.default_path ?? 'download',
    }
  }
  if (!newInst[r.media_type]) {
    newInst[r.media_type] = { role: 'debrid', label: '', host_port: null }
  }
}

async function loadRoutes() {
  try {
    routes.value = await routingApi.media()
    routes.value.forEach(initPending)
  } catch { /* intentional: route load failure is non-fatal */ }
}

async function loadInstances() {
  try {
    allInstances.value = (await routingApi.instances()) as any[]
  } catch { /* intentional: instance load failure is non-fatal */ }
}

onMounted(async () => {
  await Promise.all([loadRoutes(), loadInstances()])
})
</script>
