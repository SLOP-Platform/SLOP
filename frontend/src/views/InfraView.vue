<template>
  <div class="p-4 max-w-4xl mx-auto w-full">
    <div class="mb-4">
      <h1 class="page-title">
        Infrastructure
      </h1>
      <p class="page-subtitle">
        Auth · Tunnel · Dashboard · Management · VPN
      </p>
    </div>

    <!-- Platform core: Traefik -->
    <div class="card mb-3">
      <div class="card-body !py-2.5 flex items-center gap-3">
        <div class="w-7 h-7 rounded-lg bg-slate-100 flex items-center justify-center shrink-0 overflow-hidden">
          <span class="text-base">🔀</span>
        </div>
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-2">
            <span class="text-sm font-medium text-slate-800">Traefik</span>
            <span class="text-xs text-slate-400">Reverse proxy · core platform layer</span>
          </div>
          <div
            v-if="traefikStatus"
            class="text-xs text-slate-400 mt-0.5"
          >
            {{ traefikStatus.domain || '—' }}
          </div>
        </div>
        <div class="flex items-center gap-3 shrink-0">
          <span
            :class="['badge text-xs',
                     traefikStatus?.running ? 'badge-green' : 'badge-red']"
          >
            {{ traefikStatus?.running ? 'running' : traefikStatus ? 'stopped' : 'unknown' }}
          </span>
          <span
            v-if="traefikStatus?.version"
            class="text-xs text-slate-400 font-mono"
          >
            {{ traefikStatus.version }}
          </span>
        </div>
      </div>
    </div>

    <!-- Compact 5-slot status strip -->
    <div class="grid grid-cols-5 gap-2 mb-6">
      <button
        v-for="slot in SLOT_ORDER"
        :key="slot"
        :class="[
          'rounded-xl border px-3 py-2 text-left transition-all',
          activeSlot === slot
            ? 'border-sky-400 bg-sky-50'
            : slotMap[slot]?.status === 'active'
              ? 'border-green-300 bg-green-50 hover:border-green-400'
              : slotMap[slot]?.status === 'error'
                ? 'border-red-300 bg-red-50 hover:border-red-400'
                : slotMap[slot]?.status === 'deploying'
                  ? 'border-amber-300 bg-amber-50'
                  : 'border-slate-200 hover:border-slate-300 bg-white'
        ]"
        @click="toggleSlot(slot)"
      >
        <div class="flex items-center justify-between mb-1">
          <span class="text-base leading-none">{{ SLOT_ICONS[slot] }}</span>
          <span :class="['text-xs font-medium px-1.5 py-0.5 rounded-full', statusClass(slotMap[slot]?.status)]">
            {{ slotMap[slot]?.status ?? 'empty' }}
          </span>
        </div>
        <div class="text-xs font-medium text-slate-700 capitalize mt-1">
          {{ slot }}
        </div>
        <div class="text-xs text-slate-400 truncate mt-0.5">
          <template v-if="slot === 'tunnel'">
            {{ tunnelActiveCount === 0 ? '—' : tunnelActiveCount === 1 ? activeTunnelProviders[0]?.provider : tunnelActiveCount + ' tunnels' }}
          </template>
          <template v-else>
            {{ slotMap[slot]?.display_name || slotMap[slot]?.provider || '—' }}
          </template>
        </div>
      </button>
    </div>

    <!-- Inline accordion: slot config expands below strip -->
    <div
      v-if="activeSlot"
      class="card mb-6"
    >
      <div class="card-header flex items-center justify-between">
        <span class="font-semibold capitalize flex items-center gap-2">
          <span>{{ SLOT_ICONS[activeSlot] }}</span>
          {{ activeSlot }}
          <span :class="['text-xs font-medium px-2 py-0.5 rounded-full', statusClass(slotMap[activeSlot]?.status)]">
            {{ slotMap[activeSlot]?.status ?? 'empty' }}
          </span>
        </span>
        <button
          class="text-slate-400 hover:text-slate-600"
          @click="activeSlot = null"
        >
          ✕
        </button>
      </div>
      <div class="card-body space-y-3">
        <!-- Current provider info — single-provider slots -->
        <div
          v-if="activeSlot !== 'tunnel' && slotMap[activeSlot]?.status === 'active'"
          class="flex items-center gap-3 p-3 rounded-lg bg-green-50 border border-green-100"
        >
          <div class="w-8 h-8 flex items-center justify-center overflow-hidden">
            <img
              :src="providerIconUrl(slotMap[activeSlot]?.provider ?? '')"
              class="w-7 h-7 object-contain"
              @error="(e: Event) => { const t = e.target as HTMLImageElement; if(t){t.style.display='none'; const s=t.nextElementSibling as HTMLElement; if(s) s.style.display='block'} }"
            >
            <span class="text-base hidden">{{ PROVIDER_ICONS[slotMap[activeSlot]?.provider ?? ''] ?? '⚙️' }}</span>
          </div>
          <div class="flex-1">
            <div class="text-sm font-medium text-green-800">
              {{ slotMap[activeSlot]?.display_name || slotMap[activeSlot]?.provider }}
            </div>
            <div
              v-if="slotMap[activeSlot]?.deployed_at"
              class="text-xs text-green-600"
            >
              Since {{ new Date((slotMap[activeSlot]?.deployed_at ?? 0) * 1000).toLocaleDateString() }}
            </div>
          </div>
          <div class="flex gap-2">
            <button
              :disabled="verifying === activeSlot"
              class="btn-secondary btn-sm"
              @click="verify(activeSlot!)"
            >
              {{ verifying === activeSlot ? '…' : 'Verify' }}
            </button>
            <button
              class="btn-secondary btn-sm"
              @click="openSwap()"
            >
              Swap
            </button>
          </div>
        </div>

        <!-- Tunnel: multi-provider — show all active tunnels -->
        <div
          v-if="activeSlot === 'tunnel'"
          class="space-y-3"
        >
          <!-- Active tunnels list -->
          <div
            v-if="activeTunnelProviders.length"
            class="space-y-2"
          >
            <div class="text-xs font-medium text-slate-500 uppercase tracking-wider">
              Active tunnels
            </div>
            <div
              v-for="tp in activeTunnelProviders"
              :key="tp.provider"
              class="flex items-center gap-3 p-3 rounded-lg bg-green-50 border border-green-100"
            >
              <div class="w-8 h-8 flex items-center justify-center overflow-hidden">
                <img
                  :src="providerIconUrl(tp.provider)"
                  class="w-7 h-7 object-contain"
                  @error="(e: Event) => { const t = e.target as HTMLImageElement; if(t){t.style.display='none'; const s=t.nextElementSibling as HTMLElement; if(s) s.style.display='block'} }"
                >
                <span class="text-base hidden">{{ PROVIDER_ICONS[tp.provider] ?? '⚙️' }}</span>
              </div>
              <div class="flex-1">
                <div class="text-sm font-medium text-green-800 capitalize">
                  {{ tp.provider }}
                </div>
                <div
                  v-if="tp.deployed_at"
                  class="text-xs text-green-600"
                >
                  Active since {{ new Date((tp.deployed_at ?? 0) * 1000).toLocaleDateString() }}
                </div>
              </div>
              <div class="flex gap-2">
                <button
                  :disabled="verifying === tp.provider"
                  class="btn-secondary btn-sm text-xs"
                  @click="verifyTunnel(tp.provider)"
                >
                  {{ verifying === tp.provider ? '…' : 'Verify' }}
                </button>
                <button
                  :disabled="removingTunnel === tp.provider"
                  class="btn-secondary btn-sm text-xs text-red-500"
                  @click="removeTunnel(tp.provider)"
                >
                  {{ removingTunnel === tp.provider ? 'Removing…' : 'Remove' }}
                </button>
              </div>
            </div>
          </div>
          <!-- Add tunnel prompt -->
          <div
            v-if="filteredAvailableProviders.length"
            class="pt-1"
          >
            <div class="text-xs font-medium text-slate-500 uppercase tracking-wider mb-2">
              {{ activeTunnelProviders.length ? 'Add another tunnel' : 'Choose tunnel provider' }}
            </div>
          </div>
          <div
            v-else-if="activeTunnelProviders.length"
            class="text-xs text-slate-400 text-center py-2"
          >
            All available tunnel providers are active.
          </div>
        </div>

        <!-- Verify result -->
        <div
          v-if="verifyResults[activeSlot]"
          :class="['text-xs rounded-lg px-3 py-2', verifyResults[activeSlot].ok ? 'bg-green-50 text-green-700' : 'bg-red-50 text-red-600']"
        >
          {{ verifyResults[activeSlot].message }}
        </div>

        <!-- Provider picker — 4 across -->
        <!-- For tunnel: always show (to add more). For others: only if empty or swapping. -->
        <div v-if="activeSlot === 'tunnel' || slotMap[activeSlot]?.status !== 'active' || showingSwap">
          <label class="label mb-2">{{ showingSwap ? 'Switch to:' : activeSlot === 'tunnel' ? 'Add tunnel:' : 'Choose provider' }}</label>
          <div class="grid grid-cols-4 gap-2">
            <button
              v-for="p in filteredAvailableProviders"
              :key="p.key"
              :class="[
                'rounded-xl border p-3 text-left transition-all',
                selectedProvider?.key === p.key
                  ? 'border-sky-400 bg-sky-50'
                  : 'border-slate-200 hover:border-slate-300'
              ]"
              @click="selectProvider(p)"
            >
              <div class="w-8 h-8 mb-1 flex items-center justify-center overflow-hidden">
                <img
                  :src="providerIconUrl(p.key)"
                  class="w-7 h-7 object-contain"
                  @error="(e: Event) => { const t = e.target as HTMLImageElement; if(t){t.style.display='none'; const s=t.nextElementSibling as HTMLElement; if(s) s.style.display='block'} }"
                >
                <span class="text-xl hidden">{{ PROVIDER_ICONS[p.key] ?? '⚙️' }}</span>
              </div>
              <div class="text-xs font-medium text-slate-800 leading-tight">
                {{ p.display_name }}
              </div>
            </button>
          </div>
        </div>

        <!-- Config fields for selected provider -->
        <template v-if="selectedProvider && (activeSlot === 'tunnel' || slotMap[activeSlot]?.status !== 'active' || showingSwap)">
          <div
            v-for="field in selectedProvider.fields"
            :key="field.key"
          >
            <template v-if="field.type === 'info'">
              <div class="rounded-lg bg-sky-50 border border-sky-100 p-3 text-sm text-sky-800">
                {{ field.help }}
              </div>
            </template>
            <template v-else-if="field.type === 'checkbox'">
              <label class="flex items-center gap-3 cursor-pointer">
                <input
                  v-model="deployConfig[field.key]"
                  type="checkbox"
                  class="rounded border-slate-300"
                >
                <div>
                  <div class="text-sm font-medium text-slate-700">{{ field.label }}</div>
                  <div class="text-xs text-slate-400">{{ field.help }}</div>
                </div>
              </label>
            </template>
            <template v-else>
              <label class="label">{{ field.label }}<span
                v-if="field.required"
                class="text-red-400 ml-0.5"
              >*</span></label>
              <input
                v-model="deployConfig[field.key]"
                :type="field.secret ? 'password' : field.type === 'number' ? 'number' : 'text'"
                :placeholder="field.placeholder"
                class="input"
              >
              <p
                v-if="field.help"
                class="text-xs text-slate-400 mt-1"
              >
                {{ field.help }}
              </p>
            </template>
          </div>

          <div
            v-if="deployError"
            class="rounded-lg bg-red-50 text-red-700 text-sm p-3"
          >
            {{ deployError }}
          </div>
          <div
            v-if="deploySuccess"
            class="rounded-lg bg-green-50 text-green-700 text-sm p-3"
          >
            {{ deploySuccess }}
          </div>

          <div class="flex gap-3">
            <button
              class="btn-secondary flex-1"
              @click="showingSwap = false; selectedProvider = null; deployConfig = {}"
            >
              Cancel
            </button>
            <button
              :disabled="deploying || !selectedProvider"
              class="btn-primary flex-1"
              @click="deploy"
            >
              {{ deploying ? 'Deploying…' : showingSwap ? 'Swap provider' : activeSlot === 'tunnel' && activeTunnelProviders.length > 0 ? 'Add tunnel' : 'Deploy' }}
            </button>
          </div>
        </template>

        <!-- Empty slot prompt -->
        <div
          v-if="activeSlot !== 'tunnel' && slotMap[activeSlot]?.status !== 'active' && !availableProviders.length"
          class="text-sm text-slate-400 text-center py-4"
        >
          No providers available for this slot.
        </div>
      </div>
    </div>

    <!-- Compact provider directory -->
    <div>
      <h2 class="text-xs font-medium text-slate-400 uppercase tracking-wider mb-3">
        All providers
      </h2>
      <div class="space-y-3">
        <div
          v-for="(schemas, slotName) in schemasBySlot"
          :key="slotName"
        >
          <div class="text-xs font-medium text-slate-500 capitalize mb-1.5">
            {{ slotName }}
          </div>
          <div class="grid grid-cols-4 gap-1.5">
            <div
              v-for="p in schemas"
              :key="p.key"
              class="flex items-center gap-2 px-2 py-2 rounded-lg border border-slate-100 bg-white"
            >
              <div class="w-6 h-6 flex items-center justify-center overflow-hidden shrink-0">
                <img
                  :src="providerIconUrl(p.key)"
                  class="w-5 h-5 object-contain"
                  @error="(e: Event) => { const t = e.target as HTMLImageElement; if(t){t.style.display='none'; const s=t.nextElementSibling as HTMLElement; if(s) s.style.display='block'} }"
                >
                <span class="text-sm hidden">{{ PROVIDER_ICONS[p.key] ?? '⚙️' }}</span>
              </div>
              <span class="text-xs font-medium text-slate-700 truncate">{{ p.display_name }}</span>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { infraSlotsCache, setInfraSlotsCache } from '../appCache'
import { infra } from '../api/client'
import { useToast } from '@/composables/useToast'
import type { InfraSlot } from '../api/client'

const toast = useToast()

// Curated UI ordering — intentionally a 5-of-6 subset of the backend's deployable_slots().
// `reverse_proxy` (#990) is a deployable slot in the API but is DELIBERATELY not surfaced in
// the infra strip during the additive P1 stage: exposing a deploy/swap control for Traefik
// (the edge router every app routes through) is an operator footgun until P2 inverts compose
// label-emission to go through the slot provider. Add it here when P2 makes the slot meaningful.
const SLOT_ORDER = ['auth', 'tunnel', 'vpn', 'dashboard', 'management']
const SLOT_ICONS: Record<string, string> = { auth: '🔐', tunnel: '🌐', dashboard: '📊', management: '🐋', vpn: '🛡️' }
const PROVIDER_ICONS: Record<string, string> = {
  tinyauth: '🔐', authelia: '🔒', authentik: '🛡️',
  cloudflared: '☁️', tailscale: '🌐', headscale: '🏠',
  homepage: '📋', glance: '👁️',
  portainer: '🐋', portainer_be: '💼', dockhand: '⚓', dockge: '🎩', komodo: '🦎',
  gluetun: '🛡️',
}

function providerIconUrl(key: string): string {
  const overrides: Record<string, string> = {
    portainer_be: 'portainer', headscale: 'headscale', dockhand: 'dockhand',
  }
  const name = (overrides[key] || key).replace(/_/g, '-').toLowerCase()
  return `https://cdn.jsdelivr.net/gh/walkxcode/dashboard-icons/png/${name}.png`
}

function statusClass(status?: string) {
  if (status === 'active') return 'bg-green-100 text-green-700'
  if (status === 'error') return 'bg-red-100 text-red-600'
  return 'bg-slate-100 text-slate-500'
}

const slots = ref<InfraSlot[]>(infraSlotsCache ?? [])
const schemasBySlot = ref<Record<string, any[]>>({})
const activeSlot = ref<string | null>(null)
const traefikStatus = ref<any>(null)
const showingSwap = ref(false)
const selectedProvider = ref<any>(null)
const deployConfig = ref<Record<string, any>>({})
const deploying = ref(false)
const deployError = ref<string | null>(null)
const deploySuccess = ref<string | null>(null)
const verifying = ref<string | null>(null)
const verifyResults = ref<Record<string, { ok: boolean; message: string }>>({})

const slotMap = computed(() => {
  const m: Record<string, InfraSlot> = {}
  for (const s of slots.value) m[s.slot] = s
  return m
})

const activeTunnelProviders = computed(() => {
  const tunnelSlot = slotMap.value['tunnel'] as any
  if (!tunnelSlot?.providers) return []
  return tunnelSlot.providers.filter((p: any) => p.status === 'active')
})

const tunnelActiveCount = computed(() => activeTunnelProviders.value.length)

const activeTunnelProviderKeys = computed(() =>
  new Set(activeTunnelProviders.value.map((p: any) => p.provider))
)

const availableProviders = computed(() => {
  if (!activeSlot.value) return []
  const all = schemasBySlot.value[activeSlot.value] ?? []
  if (showingSwap.value) {
    const current = slotMap.value[activeSlot.value]?.provider
    return all.filter(p => p.key !== current)
  }
  return all
})

// For tunnel: filter out already-active providers
const filteredAvailableProviders = computed(() => {
  if (activeSlot.value !== 'tunnel') return availableProviders.value
  return availableProviders.value.filter(
    (p: any) => !activeTunnelProviderKeys.value.has(p.key)
  )
})

const removingTunnel = ref<string | null>(null)

function toggleSlot(slot: string) {
  if (activeSlot.value === slot) {
    activeSlot.value = null
    showingSwap.value = false
    selectedProvider.value = null
  } else {
    activeSlot.value = slot
    showingSwap.value = false
    selectedProvider.value = null
    deployConfig.value = {}
    deployError.value = null
    deploySuccess.value = null
  }
}

function openSwap() {
  showingSwap.value = true
  selectedProvider.value = null
  deployConfig.value = {}
}

function selectProvider(p: any) {
  selectedProvider.value = p
  deployConfig.value = {}
}

async function deploy() {
  if (!activeSlot.value || !selectedProvider.value) return
  deploying.value = true
  deployError.value = null
  deploySuccess.value = null

  try {
    const slot = activeSlot.value
    const providerKey = selectedProvider.value.key
    const cfg = { ...deployConfig.value }

    if (showingSwap.value) {
      await infra.swap(slot, providerKey, cfg)
    } else {
      await infra.deploy(slot, providerKey, cfg)
    }

    const msg = `${selectedProvider.value.display_name} deployed successfully.`
    deploySuccess.value = msg
    toast.success(msg)
    slots.value = await infra.slots()
    showingSwap.value = false
    selectedProvider.value = null
  } catch (e) {
    const msg = e instanceof Error ? e.message : String(e)
    deployError.value = msg
    toast.error('Deploy failed.', msg)
  } finally {
    deploying.value = false
  }
}

async function verifyTunnel(providerKey: string) {
  verifying.value = providerKey
  try {
    const { data: d } = await infra.tunnelVerify(providerKey)
    verifyResults.value[providerKey] = { ok: d.ok, message: d.message }
    toast[d.ok ? 'success' : 'error'](d.message)
  } catch (e) {
    toast.error('Verify failed.', String(e))
  } finally {
    verifying.value = null
  }
}

async function removeTunnel(providerKey: string) {
  if (!confirm(`Remove ${providerKey} tunnel?`)) return
  removingTunnel.value = providerKey
  try {
    const { data: d } = await infra.tunnelRemove(providerKey)
    if (d.ok) {
      toast.success(`${providerKey} tunnel removed.`)
      slots.value = await infra.slots()
    } else {
      toast.error(`Remove failed: ${d.message}`)
    }
  } catch (e) {
    toast.error('Remove failed.', String(e))
  } finally {
    removingTunnel.value = null
  }
}

async function verify(slot: string) {
  verifying.value = slot
  verifyResults.value[slot] = { ok: false, message: 'Checking…' }
  try {
    const { ok: httpOk, status, data } = await infra.slotVerify(slot)
    const ok = data.ok ?? httpOk
    const msg = data.message ?? (httpOk ? 'Provider is running.' : `Error: ${status}`)
    verifyResults.value[slot] = { ok, message: msg }
    if (ok) toast.success(`${slot}: ${msg}`)
    else toast.error(`${slot} verify failed.`, msg)
  } catch (e) {
    verifyResults.value[slot] = { ok: false, message: `Cannot reach server: ${String(e)}` }
  } finally {
    verifying.value = null
    setTimeout(() => { if (verifyResults.value[slot]?.ok) delete verifyResults.value[slot] }, 5000)
  }
}

onMounted(async () => {
  const [slotData, ...schemaResults] = await Promise.allSettled([
    infra.slots(),
    ...SLOT_ORDER.map(s => infra.providerSchema(s).catch(() => []))
  ])
  if (slotData.status === 'fulfilled') { slots.value = slotData.value; setInfraSlotsCache(slotData.value) }
  const schemas: Record<string, any[]> = {}
  SLOT_ORDER.forEach((slot, i) => {
    const res = schemaResults[i]
    if (res.status === 'fulfilled') schemas[slot] = res.value as any[]
  })
  schemasBySlot.value = schemas
})
</script>
