<template>
  <div class="p-6 max-w-screen-xl mx-auto space-y-6">
    <!-- Header -->
    <div class="flex items-center justify-between">
      <div>
        <h1 class="text-xl font-semibold text-slate-900">
          Topology & Coverage
        </h1>
        <p class="text-sm text-slate-500 mt-0.5">
          Live map of every route, table, provider, manifest, step, and view — with test coverage overlaid.
        </p>
      </div>
      <div class="flex items-center gap-3">
        <span
          v-if="data"
          class="text-xs text-slate-400"
        >
          commit {{ data.commit }} · generated {{ generatedAgo }}
        </span>
        <button
          :disabled="loading"
          class="btn-secondary btn-sm text-xs flex items-center gap-1.5"
          @click="refresh"
        >
          <span
            v-if="loading"
            class="w-3 h-3 border-2 border-slate-300 border-t-sky-500 rounded-full animate-spin"
          />
          <span v-else>↻</span>
          Refresh
        </button>
      </div>
    </div>

    <!-- Loading skeleton -->
    <div
      v-if="loading && !data"
      class="space-y-4"
    >
      <div class="grid grid-cols-4 gap-4">
        <div
          v-for="n in 4"
          :key="n"
          class="h-24 bg-slate-100 rounded-xl animate-pulse"
        />
      </div>
      <div class="h-96 bg-slate-100 rounded-xl animate-pulse" />
    </div>

    <!-- Error -->
    <div
      v-else-if="error"
      class="rounded-xl border border-red-200 bg-red-50 p-6 text-sm text-red-700"
    >
      {{ error }}
      <button
        class="ml-3 underline"
        @click="refresh"
      >
        Retry
      </button>
    </div>

    <template v-else-if="data">
      <!-- Summary cards -->
      <div class="grid grid-cols-2 md:grid-cols-4 gap-4">
        <div class="rounded-xl border border-slate-200 bg-white p-4">
          <div
            class="text-2xl font-bold"
            :class="coveragePctColor"
          >
            {{ data.summary.coverage_pct }}%
          </div>
          <div class="text-xs text-slate-500 mt-1">
            Overall coverage
          </div>
          <div class="text-xs text-slate-400">
            {{ data.summary.covered }}/{{ data.summary.total }} nodes
          </div>
        </div>
        <div class="rounded-xl border border-slate-200 bg-white p-4">
          <div
            class="text-2xl font-bold"
            :class="data.summary.critical_gaps ? 'text-red-600' : 'text-green-600'"
          >
            {{ data.summary.critical_gaps }}
          </div>
          <div class="text-xs text-slate-500 mt-1">
            Critical gaps
          </div>
          <div class="text-xs text-slate-400">
            {{ data.summary.high_gaps }} high-risk
          </div>
        </div>
        <div class="rounded-xl border border-slate-200 bg-white p-4">
          <div class="text-2xl font-bold text-slate-700">
            {{ data.nodes.length }}
          </div>
          <div class="text-xs text-slate-500 mt-1">
            Total nodes
          </div>
          <div class="text-xs text-slate-400">
            {{ Object.keys(data.summary.by_kind).length }} kinds
          </div>
        </div>
        <div class="rounded-xl border border-slate-200 bg-white p-4">
          <div class="text-2xl font-bold text-slate-700">
            {{ data.known_bugs?.filter(b => b.fixed).length ?? 0 }}
          </div>
          <div class="text-xs text-slate-500 mt-1">
            Bugs fixed via tests
          </div>
          <div class="text-xs text-slate-400">
            tracked regressions
          </div>
        </div>
      </div>

      <!-- Kind breakdown + filter -->
      <div class="rounded-xl border border-slate-200 bg-white overflow-hidden">
        <div class="flex items-center gap-2 px-4 py-3 border-b border-slate-100 flex-wrap">
          <button
            v-for="kind in allKinds"
            :key="kind"
            :class="['text-xs px-2.5 py-1 rounded-full border transition-colors',
                     activeKinds.has(kind)
                       ? kindColors[kind]?.active ?? 'bg-sky-100 border-sky-300 text-sky-700'
                       : 'bg-white border-slate-200 text-slate-400']"
            @click="toggleKind(kind)"
          >
            {{ kindLabel(kind) }}
            <span class="ml-1 opacity-70">{{ data.summary.by_kind[kind]?.total ?? 0 }}</span>
          </button>
          <div class="ml-auto flex items-center gap-2">
            <input
              v-model="search"
              type="text"
              placeholder="Search nodes…"
              class="input text-xs w-48"
            >
            <button
              :class="['text-xs px-2.5 py-1 rounded-full border transition-colors',
                       showOnlyUncovered ? 'bg-red-50 border-red-200 text-red-600' : 'bg-white border-slate-200 text-slate-500']"
              @click="toggleCoverageFilter"
            >
              {{ showOnlyUncovered ? '✗ Gaps only' : '⬡ All nodes' }}
            </button>
          </div>
        </div>

        <!-- Node table -->
        <div class="overflow-x-auto">
          <table class="w-full text-sm">
            <thead>
              <tr class="bg-slate-50 text-xs text-slate-500">
                <th class="px-4 py-2 text-left font-medium">
                  Node
                </th>
                <th class="px-4 py-2 text-left font-medium">
                  Kind
                </th>
                <th class="px-4 py-2 text-left font-medium">
                  Risk
                </th>
                <th class="px-4 py-2 text-left font-medium">
                  Coverage
                </th>
                <th class="px-4 py-2 text-left font-medium">
                  Test types
                </th>
                <th class="px-4 py-2 text-left font-medium">
                  File
                </th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="node in filteredNodes"
                :key="node.id"
                :class="['border-t border-slate-50 hover:bg-slate-50 cursor-pointer transition-colors',
                         selectedNode?.id === node.id ? 'bg-sky-50' : '']"
                @click="selectNode(node)"
              >
                <td class="px-4 py-2">
                  <div
                    class="font-mono text-xs text-slate-700 truncate max-w-xs"
                    :title="node.label"
                  >
                    {{ node.label }}
                  </div>
                  <div
                    v-if="node.detail"
                    class="text-xs text-slate-400 truncate max-w-xs"
                  >
                    {{ node.detail }}
                  </div>
                </td>
                <td class="px-4 py-2">
                  <span :class="['text-xs px-1.5 py-0.5 rounded', kindColors[node.kind]?.badge ?? 'bg-slate-100 text-slate-600']">
                    {{ node.kind }}
                  </span>
                </td>
                <td class="px-4 py-2">
                  <span :class="['text-xs font-medium', riskColor(node.risk)]">{{ node.risk }}</span>
                </td>
                <td class="px-4 py-2">
                  <div class="flex items-center gap-1.5">
                    <div
                      :class="['w-2 h-2 rounded-full shrink-0',
                               node.covered ? 'bg-green-400' : 'bg-red-400']"
                    />
                    <span class="text-xs text-slate-500">
                      {{ node.covered ? `${node.test_count} refs` : 'Not covered' }}
                    </span>
                  </div>
                </td>
                <td class="px-4 py-2">
                  <div class="flex gap-1 flex-wrap">
                    <span
                      v-for="t in node.test_kinds"
                      :key="t"
                      :class="['text-xs px-1.5 py-0.5 rounded', testKindColor(t)]"
                    >
                      {{ t }}
                    </span>
                  </div>
                </td>
                <td class="px-4 py-2">
                  <span
                    class="text-xs text-slate-400 font-mono truncate max-w-xs block"
                    :title="node.file"
                  >
                    {{ node.file.split('/').slice(-2).join('/') }}
                  </span>
                </td>
              </tr>
              <tr v-if="filteredNodes.length === 0">
                <td
                  colspan="6"
                  class="px-4 py-8 text-center text-sm text-slate-400"
                >
                  No nodes match the current filter.
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div class="px-4 py-2 border-t border-slate-100 text-xs text-slate-400">
          Showing {{ filteredNodes.length }} of {{ data.nodes.length }} nodes
        </div>
      </div>

      <!-- Detail panel -->
      <div
        v-if="selectedNode"
        class="rounded-xl border border-slate-200 bg-white p-5 space-y-3"
      >
        <div class="flex items-start justify-between gap-4">
          <div>
            <div class="font-mono text-sm font-medium text-slate-800">
              {{ selectedNode.label }}
            </div>
            <div class="text-xs text-slate-500 mt-0.5">
              {{ selectedNode.detail }}
            </div>
          </div>
          <button
            class="text-slate-400 hover:text-slate-600 text-lg"
            @click="selectedNode = null"
          >
            ✕
          </button>
        </div>
        <div class="grid grid-cols-3 gap-4 text-xs">
          <div>
            <div class="text-slate-400 mb-1">
              Kind
            </div>
            <span :class="['px-1.5 py-0.5 rounded', kindColors[selectedNode.kind]?.badge ?? 'bg-slate-100 text-slate-600']">
              {{ selectedNode.kind }}
            </span>
          </div>
          <div>
            <div class="text-slate-400 mb-1">
              Risk
            </div>
            <span :class="['font-medium', riskColor(selectedNode.risk)]">{{ selectedNode.risk }}</span>
          </div>
          <div>
            <div class="text-slate-400 mb-1">
              Coverage
            </div>
            <span :class="selectedNode.covered ? 'text-green-600' : 'text-red-500'">
              {{ selectedNode.covered ? `✓ Covered (${selectedNode.test_count} refs)` : '✗ Not covered' }}
            </span>
          </div>
        </div>
        <div
          v-if="selectedNode.test_files.length"
          class="text-xs"
        >
          <div class="text-slate-400 mb-1">
            Test files
          </div>
          <div class="flex gap-1 flex-wrap">
            <span
              v-for="f in selectedNode.test_files"
              :key="f"
              class="bg-slate-100 text-slate-600 px-2 py-0.5 rounded font-mono"
            >{{ f }}</span>
          </div>
        </div>
        <div
          v-if="selectedNode.depends_on.length"
          class="text-xs"
        >
          <div class="text-slate-400 mb-1">
            Depends on
          </div>
          <div class="text-slate-600">
            {{ selectedNode.depends_on.slice(0, 5).join(', ') }}
          </div>
        </div>
        <div class="text-xs">
          <div class="text-slate-400 mb-1">
            Source
          </div>
          <span class="font-mono text-slate-600">{{ selectedNode.file }}:{{ selectedNode.line }}</span>
        </div>
        <div
          v-if="!selectedNode.covered"
          class="bg-amber-50 border border-amber-200 rounded p-3 text-xs text-amber-700"
        >
          <strong>Gap:</strong> {{ gapSuggestion(selectedNode) }}
        </div>
      </div>

      <!-- Known bugs timeline -->
      <div
        v-if="data.known_bugs?.length"
        class="rounded-xl border border-slate-200 bg-white overflow-hidden"
      >
        <div class="px-4 py-3 border-b border-slate-100 flex items-center justify-between">
          <h2 class="text-sm font-medium text-slate-700">
            Bugs Fixed via Testing
          </h2>
          <span class="text-xs text-slate-400">{{ data.known_bugs.filter(b => b.fixed).length }} fixed</span>
        </div>
        <div class="divide-y divide-slate-50">
          <div
            v-for="bug in data.known_bugs"
            :key="bug.id"
            class="px-4 py-3 flex items-start gap-3"
          >
            <span :class="['text-base shrink-0 mt-0.5', bug.fixed ? 'text-green-500' : 'text-amber-400']">
              {{ bug.fixed ? '✓' : '○' }}
            </span>
            <div class="flex-1 min-w-0">
              <div class="text-xs text-slate-700">
                {{ bug.desc }}
              </div>
              <div class="text-xs text-slate-400 mt-0.5 font-mono">
                {{ bug.id }}
              </div>
            </div>
            <div class="text-xs font-mono text-slate-400 shrink-0">
              {{ bug.commit?.slice(0,7) ?? '' }}
            </div>
          </div>
        </div>
      </div>
    </template>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'

interface CoverageNode {
  id: string
  kind: string
  label: string
  detail: string
  file: string
  line: number
  covered: boolean
  test_count: number
  test_kinds: string[]
  test_files: string[]
  risk: string
  depends_on: string[]
  notes?: string
}

interface CoverageData {
  generated_at: number
  commit: string
  nodes: CoverageNode[]
  summary: any
  gaps: any[]
  known_bugs: any[]
}

const data = ref<CoverageData | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
const selectedNode = ref<CoverageNode | null>(null)
const search = ref('')
const showOnlyUncovered = ref(false)
const activeKinds = ref(new Set(['route', 'table', 'provider', 'manifest', 'step', 'view', 'tool']))

const allKinds = ['route', 'table', 'provider', 'manifest', 'step', 'view', 'tool', 'rule']

const kindColors: Record<string, { active: string; badge: string }> = {
  route:    { active: 'bg-sky-100 border-sky-300 text-sky-700',      badge: 'bg-sky-100 text-sky-700' },
  table:    { active: 'bg-violet-100 border-violet-300 text-violet-700', badge: 'bg-violet-100 text-violet-700' },
  provider: { active: 'bg-orange-100 border-orange-300 text-orange-700', badge: 'bg-orange-100 text-orange-700' },
  manifest: { active: 'bg-green-100 border-green-300 text-green-700',  badge: 'bg-green-100 text-green-700' },
  step:     { active: 'bg-pink-100 border-pink-300 text-pink-700',    badge: 'bg-pink-100 text-pink-700' },
  view:     { active: 'bg-amber-100 border-amber-300 text-amber-700', badge: 'bg-amber-100 text-amber-700' },
  tool:     { active: 'bg-teal-100 border-teal-300 text-teal-700',   badge: 'bg-teal-100 text-teal-700' },
  rule:     { active: 'bg-red-100 border-red-300 text-red-700',     badge: 'bg-red-100 text-red-700' },
}

function kindLabel(kind: string): string {
  const labels: Record<string, string> = {
    route: 'Routes', table: 'Tables', provider: 'Providers',
    manifest: 'Manifests', step: 'Wizard Steps', view: 'Views', tool: 'Tools'
  }
  return labels[kind] ?? kind
}

function riskColor(risk: string): string {
  return { critical: 'text-red-600', high: 'text-amber-600', medium: 'text-slate-500', low: 'text-slate-400' }[risk] ?? 'text-slate-500'
}

function testKindColor(kind: string): string {
  return { e2e: 'bg-green-100 text-green-700', runtime: 'bg-sky-100 text-sky-700', static: 'bg-slate-100 text-slate-500' }[kind] ?? 'bg-slate-100 text-slate-500'
}

const coveragePctColor = computed(() => {
  const p = data.value?.summary.coverage_pct ?? 0
  return p >= 80 ? 'text-green-600' : p >= 50 ? 'text-amber-500' : 'text-red-600'
})

const generatedAgo = computed(() => {
  if (!data.value) return ''
  const age = Math.floor(Date.now() / 1000 - data.value.generated_at)
  if (age < 60) return 'just now'
  if (age < 3600) return `${Math.floor(age / 60)}m ago`
  return `${Math.floor(age / 3600)}h ago`
})

const filteredNodes = computed(() => {
  if (!data.value) return []
  return data.value.nodes.filter(n => {
    if (!activeKinds.value.has(n.kind)) return false
    if (showOnlyUncovered.value && n.covered) return false
    if (search.value) {
      const q = search.value.toLowerCase()
      return n.label.toLowerCase().includes(q) || n.file.toLowerCase().includes(q) || n.kind.includes(q)
    }
    return true
  })
})

function toggleKind(kind: string) {
  const s = new Set(activeKinds.value)
  if (s.has(kind)) { if (s.size > 1) s.delete(kind) } else s.add(kind)
  activeKinds.value = s
}

function toggleCoverageFilter() { showOnlyUncovered.value = !showOnlyUncovered.value }

function selectNode(node: CoverageNode) {
  selectedNode.value = selectedNode.value?.id === node.id ? null : node
}

function gapSuggestion(node: CoverageNode): string {
  const hints: Record<string, string> = {
    route: `Add test: client.${node.label.split(' ')[0].toLowerCase()}('${node.label.split(' ')[1]}') → assert 200 + JSON shape`,
    table: `Add test: write a record to '${node.label}' → read it back → assert fields match`,
    provider: `Add test: ${node.label}().deploy(cfg) with compose failure → assert ProviderResult not NameError`,
    step: `Add test: ${node.detail}(wizard_input) → assert StepResult(.ok + .step fields)`,
    view: `Add frontend contract test: verify all API calls in ${node.label}View.vue exist as routes`,
    tool: `Add test: run ${node.label} → assert exit code and output structure`,
    manifest: `Add test: validate ${node.label} YAML has required fields, valid port types`,
  }
  return hints[node.kind] ?? `Add behavioral test for ${node.kind}: ${node.label}`
}

async function refresh() {
  loading.value = true
  error.value = null
  try {
    const r = await fetch('/api/coverage')
    if (!r.ok) throw new Error(`${r.status}: ${r.statusText}`)
    data.value = await r.json()
  } catch (e) {
    error.value = `Failed to load coverage map: ${e}. Run ms-coverage on the server to generate.`
  } finally {
    loading.value = false
  }
}

onMounted(refresh)
</script>
