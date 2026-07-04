<template>
  <div class="p-4 max-w-4xl mx-auto w-full">
    <div class="mb-4">
      <h1 class="page-title">
        Observability
      </h1>
      <p class="page-subtitle">
        Audit log and unified event timeline
      </p>
    </div>

    <!-- Tab navigation -->
    <div class="flex mb-4 border-b border-slate-200">
      <button
        v-for="tab in tabs"
        :key="tab.id"
        :class="['px-4 py-2 text-sm font-medium border-b-2 -mb-px transition-colors',
                 activeTab === tab.id
                   ? 'border-orange-500 text-orange-600'
                   : 'border-transparent text-slate-500 hover:text-slate-700']"
        @click="activeTab = tab.id"
      >
        {{ tab.label }}
      </button>
    </div>

    <!-- ── Audit Log tab ─────────────────────────────────────────────── -->
    <div v-show="activeTab === 'audit'">
      <div class="flex items-center justify-between mb-3">
        <div class="flex items-center gap-2">
          <select
            v-model="auditActor"
            class="input text-xs w-36"
            @change="loadAuditLog"
          >
            <option value="">
              All actors
            </option>
            <option value="api">
              api
            </option>
          </select>
          <input
            v-model="auditAction"
            type="text"
            class="input text-xs w-48"
            placeholder="Filter by action…"
            @keydown.enter="loadAuditLog"
          >
        </div>
        <button
          :disabled="auditLoading"
          class="btn-secondary btn-sm text-xs flex items-center gap-1.5"
          @click="loadAuditLog"
        >
          <span
            v-if="auditLoading"
            class="w-3 h-3 border-2 border-slate-300 border-t-sky-500 rounded-full animate-spin"
          />
          <span v-else>↻</span>
          Refresh
        </button>
      </div>

      <!-- Loading skeleton -->
      <div
        v-if="auditLoading && !auditRows.length"
        class="space-y-1"
      >
        <div
          v-for="n in 5"
          :key="n"
          class="card animate-pulse"
        >
          <div class="card-body !py-2.5 flex items-center gap-3">
            <div class="h-3 bg-slate-100 rounded w-20 shrink-0" />
            <div class="h-3 bg-slate-100 rounded w-16 shrink-0" />
            <div class="h-3 bg-slate-100 rounded flex-1" />
          </div>
        </div>
      </div>

      <!-- Error -->
      <div
        v-else-if="auditError"
        class="card card-body text-sm text-red-600 text-center py-6"
      >
        {{ auditError }}
        <button
          class="ml-2 underline text-sky-600"
          @click="loadAuditLog"
        >
          Retry
        </button>
      </div>

      <!-- Empty state -->
      <div
        v-else-if="!auditRows.length"
        class="card card-body text-center py-8 text-slate-400 text-sm"
      >
        No audit log entries yet.
      </div>

      <!-- Rows -->
      <div
        v-else
        class="card overflow-hidden"
      >
        <table class="w-full text-xs">
          <thead>
            <tr class="bg-slate-50 border-b border-slate-100 text-left">
              <th class="px-3 py-2 font-medium text-slate-500 w-32">
                Time
              </th>
              <th class="px-3 py-2 font-medium text-slate-500 w-20">
                Status
              </th>
              <th class="px-3 py-2 font-medium text-slate-500">
                Action
              </th>
              <th class="px-3 py-2 font-medium text-slate-500 w-24">
                Actor
              </th>
              <th class="px-3 py-2 font-medium text-slate-500 w-28">
                Resource
              </th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="row in auditRows"
              :key="row.id"
              class="border-b border-slate-50 last:border-0 hover:bg-slate-50 transition-colors"
            >
              <td class="px-3 py-2 text-slate-400 whitespace-nowrap">
                {{ formatAge(row.ts) }}
              </td>
              <td class="px-3 py-2">
                <span
                  :class="['badge text-xs',
                           row.response_status >= 500 ? 'badge-red' :
                           row.response_status >= 400 ? 'badge-yellow' :
                           row.response_status >= 200 ? 'badge-green' : 'badge-gray']"
                >
                  {{ row.response_status }}
                </span>
              </td>
              <td
                class="px-3 py-2 font-mono text-slate-700 truncate max-w-xs"
                :title="row.action"
              >
                {{ row.action }}
              </td>
              <td class="px-3 py-2 text-slate-500 truncate">
                {{ row.actor || '—' }}
              </td>
              <td
                class="px-3 py-2 text-slate-400 font-mono truncate"
                :title="row.resource_id ?? undefined"
              >
                {{ row.resource_id || '—' }}
              </td>
            </tr>
          </tbody>
        </table>
        <div class="px-3 py-2 border-t border-slate-100 text-xs text-slate-400">
          {{ auditRows.length }} entries
        </div>
      </div>
    </div>

    <!-- ── Timeline tab ──────────────────────────────────────────────── -->
    <div v-show="activeTab === 'timeline'">
      <div class="flex items-center justify-between mb-3">
        <!-- Type filter -->
        <div class="flex items-center gap-1 flex-wrap">
          <button
            v-for="f in typeFilters"
            :key="f.value"
            :class="['text-xs px-3 py-1 rounded-full border transition-colors',
                     timelineTypeFilter === f.value
                       ? 'bg-slate-700 text-white border-slate-700'
                       : 'bg-white text-slate-500 border-slate-200 hover:border-slate-400']"
            @click="setTypeFilter(f.value)"
          >
            {{ f.label }}
          </button>
        </div>
        <button
          :disabled="timelineLoading"
          class="btn-secondary btn-sm text-xs flex items-center gap-1.5 shrink-0"
          @click="loadTimeline"
        >
          <span
            v-if="timelineLoading"
            class="w-3 h-3 border-2 border-slate-300 border-t-sky-500 rounded-full animate-spin"
          />
          <span v-else>↻</span>
          Refresh
        </button>
      </div>

      <!-- Loading skeleton -->
      <div
        v-if="timelineLoading && !timelineEvents.length"
        class="space-y-1"
      >
        <div
          v-for="n in 5"
          :key="n"
          class="card animate-pulse"
        >
          <div class="card-body !py-2.5 flex items-center gap-3">
            <div class="h-3 bg-slate-100 rounded w-20 shrink-0" />
            <div class="h-4 bg-slate-100 rounded-full w-20 shrink-0" />
            <div class="h-3 bg-slate-100 rounded flex-1" />
          </div>
        </div>
      </div>

      <!-- Error -->
      <div
        v-else-if="timelineError"
        class="card card-body text-sm text-red-600 text-center py-6"
      >
        {{ timelineError }}
        <button
          class="ml-2 underline text-sky-600"
          @click="loadTimeline"
        >
          Retry
        </button>
      </div>

      <!-- Empty state -->
      <div
        v-else-if="!timelineEvents.length"
        class="card card-body text-center py-8 text-slate-400 text-sm"
      >
        No timeline events yet.
      </div>

      <!-- Event list -->
      <div
        v-else
        class="space-y-1"
      >
        <div
          v-for="event in timelineEvents"
          :key="`${event.type}-${event.ts}-${event.source_id}`"
          class="card card-body !py-2.5 flex items-start gap-3"
        >
          <!-- Timestamp -->
          <span class="text-xs text-slate-400 whitespace-nowrap shrink-0 w-20 pt-0.5">
            {{ formatAge(event.ts) }}
          </span>
          <!-- Type badge -->
          <span :class="['badge text-xs shrink-0 whitespace-nowrap', typeBadgeClass(event)]">
            {{ typeLabel(event.type) }}
          </span>
          <!-- Summary -->
          <span class="text-xs text-slate-700 flex-1 leading-relaxed min-w-0">
            {{ event.summary }}
          </span>
        </div>
      </div>

      <div
        v-if="timelineEvents.length"
        class="mt-2 text-xs text-slate-400 text-right"
      >
        {{ timelineEvents.length }} events
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'

// ── Types ──────────────────────────────────────────────────────────────────

interface AuditRow {
  id: number
  ts: number
  actor: string | null
  action: string
  resource_id: string | null
  request_body_hash: string | null
  response_status: number
  correlation_id: string | null
}

interface TimelineEvent {
  ts: number
  type: 'api_mutation' | 'operation' | 'health_check' | 'llm_call'
  source_id: number
  summary: string
  detail: Record<string, unknown>
}

// ── Tab state ──────────────────────────────────────────────────────────────

const tabs = [
  { id: 'audit', label: 'Audit Log' },
  { id: 'timeline', label: 'Timeline' },
]
const activeTab = ref('audit')

// Fetch timeline when tab first becomes active
watch(activeTab, (tab) => {
  if (tab === 'timeline' && !timelineLoaded.value) loadTimeline()
})

// ── Helpers ────────────────────────────────────────────────────────────────

function formatAge(ts: number): string {
  const diff = Math.floor(Date.now() / 1000) - ts
  if (diff < 60) return `${diff}s ago`
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`
  return `${Math.floor(diff / 86400)}d ago`
}

// ── Audit Log ──────────────────────────────────────────────────────────────

const auditRows = ref<AuditRow[]>([])
const auditLoading = ref(false)
const auditError = ref<string | null>(null)
const auditActor = ref('')
const auditAction = ref('')

async function loadAuditLog() {
  auditLoading.value = true
  auditError.value = null
  try {
    const params = new URLSearchParams({ limit: '200' })
    if (auditActor.value) params.set('actor', auditActor.value)
    if (auditAction.value) params.set('action', auditAction.value)
    const r = await fetch(`/api/v1/audit?${params}`)
    if (!r.ok) throw new Error(`${r.status}: ${r.statusText}`)
    const data = await r.json()
    auditRows.value = data.rows ?? []
  } catch (e) {
    auditError.value = `Could not load audit log: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    auditLoading.value = false
  }
}

// ── Timeline ───────────────────────────────────────────────────────────────

const timelineEvents = ref<TimelineEvent[]>([])
const timelineLoading = ref(false)
const timelineError = ref<string | null>(null)
const timelineLoaded = ref(false)
const timelineTypeFilter = ref('')

const typeFilters = [
  { value: '', label: 'All' },
  { value: 'api_mutation', label: 'API Mutations' },
  { value: 'operation', label: 'Operations' },
  { value: 'health_check', label: 'Health Checks' },
  { value: 'llm_call', label: 'LLM Calls' },
]

function setTypeFilter(value: string) {
  timelineTypeFilter.value = value
  loadTimeline()
}

function typeLabel(type: string): string {
  const labels: Record<string, string> = {
    api_mutation: 'api',
    operation: 'operation',
    health_check: 'health',
    llm_call: 'llm',
  }
  return labels[type] ?? type
}

function typeBadgeClass(event: TimelineEvent): string {
  switch (event.type) {
    case 'api_mutation':
      return 'badge-gray'
    case 'operation':
      return 'badge-blue'
    case 'health_check': {
      // Red if summary contains failure signal, green otherwise
      const s = event.summary.toLowerCase()
      return s.includes('fail') || s.includes('error') ? 'badge-red' : 'badge-green'
    }
    case 'llm_call':
      // Purple — inline Tailwind since no badge-purple utility is defined
      return 'bg-violet-100 text-violet-700'
    default:
      return 'badge-gray'
  }
}

async function loadTimeline() {
  timelineLoading.value = true
  timelineError.value = null
  try {
    const params = new URLSearchParams({ limit: '200' })
    if (timelineTypeFilter.value) params.set('types', timelineTypeFilter.value)
    const r = await fetch(`/api/v1/timeline?${params}`)
    if (!r.ok) throw new Error(`${r.status}: ${r.statusText}`)
    const data = await r.json()
    timelineEvents.value = data.events ?? []
    timelineLoaded.value = true
  } catch (e) {
    timelineError.value = `Could not load timeline: ${e instanceof Error ? e.message : String(e)}`
  } finally {
    timelineLoading.value = false
  }
}

// ── Mount ──────────────────────────────────────────────────────────────────

onMounted(() => {
  loadAuditLog()
})
</script>
