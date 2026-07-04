<template>
  <div
    v-if="wizard.show && !dismissed"
    class="mb-4"
  >
    <div class="card border-2 border-sky-200 bg-gradient-to-br from-white to-sky-50">
      <div class="card-body !pb-2">
        <!-- Header -->
        <div class="flex items-start justify-between mb-3">
          <div>
            <div class="flex items-center gap-2">
              <span class="text-lg">🚀</span>
              <h2 class="font-semibold text-slate-900">
                Welcome to S.L.O.P.
              </h2>
            </div>
            <p class="text-xs text-slate-500 mt-0.5">
              {{ wizard.required_done }} of {{ wizard.total_required }} required steps complete
            </p>
          </div>
          <div class="flex items-center gap-2 shrink-0">
            <div class="text-xs text-slate-400">
              {{ wizard.percent }}%
            </div>
            <div class="w-24 h-1.5 bg-slate-100 rounded-full overflow-hidden">
              <div
                class="h-full bg-sky-500 rounded-full transition-all"
                :style="`width:${wizard.percent}%`"
              />
            </div>
            <button
              class="text-slate-300 hover:text-slate-500 ml-1 text-lg leading-none"
              @click="dismiss"
            >
              ✕
            </button>
          </div>
        </div>

        <!-- Phase grid -->
        <div class="grid grid-cols-4 gap-1.5 mb-2">
          <div
            v-for="phase in wizard.phases"
            :key="phase.id"
            :class="['rounded-lg border px-2.5 py-2 cursor-pointer transition-all',
                     phase.status === 'complete' ? 'border-green-200 bg-green-50' :
                     phase.status === 'skipped' ? 'border-slate-100 bg-slate-50 opacity-60' :
                     phase.id === activePhase ? 'border-sky-300 bg-sky-50 ring-1 ring-sky-300' :
                     'border-slate-200 bg-white hover:border-sky-200']"
            @click="activePhase = activePhase === phase.id ? null : phase.id"
          >
            <div class="flex items-center gap-1.5">
              <span class="text-sm">{{ phaseIcon(phase) }}</span>
              <div class="flex-1 min-w-0">
                <div class="text-xs font-medium text-slate-800 leading-tight truncate">
                  {{ phase.label }}
                </div>
                <div
                  v-if="phase.optional"
                  class="text-xs text-slate-400 leading-none"
                >
                  optional
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- Expanded phase detail -->
        <div
          v-if="activePhase"
          class="border-t border-sky-100 pt-2 mb-1"
        >
          <div
            v-for="phase in wizard.phases.filter((p: any) => p.id === activePhase)"
            :key="phase.id"
            class="flex items-center gap-3"
          >
            <div class="flex-1">
              <div class="text-sm font-medium text-slate-800">
                {{ phase.label }}
              </div>
              <div class="text-xs text-slate-500">
                {{ phase.description }}
              </div>
            </div>
            <div class="flex gap-1.5 shrink-0">
              <button
                v-if="phase.status !== 'complete'"
                class="btn-primary btn-sm text-xs"
                @click="markPhase(phase.id, 'complete')"
              >
                Mark done
              </button>
              <RouterLink
                v-if="phase.route"
                :to="phase.route"
                class="btn-secondary btn-sm text-xs"
              >
                Go →
              </RouterLink>
              <button
                v-if="phase.optional && phase.status !== 'skipped'"
                class="btn-secondary btn-sm text-xs text-slate-400"
                @click="markPhase(phase.id, 'skipped')"
              >
                Skip
              </button>
              <button
                v-if="phase.status !== 'pending'"
                class="text-xs text-slate-400 hover:text-slate-600 px-1"
                @click="markPhase(phase.id, 'pending')"
              >
                Reset
              </button>
            </div>
          </div>
        </div>

        <!-- Footer actions -->
        <div class="flex items-center justify-between pt-1 border-t border-sky-100">
          <button
            class="text-xs text-slate-400 hover:text-slate-600"
            @click="dismiss"
          >
            Dismiss — I'll set up manually
          </button>
          <span class="text-xs text-slate-400">
            Re-open any time from Settings
          </span>
        </div>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { RouterLink } from 'vue-router'

const wizard = ref<any>({ show: false, phases: [], percent: 0, required_done: 0, total_required: 0 })
const dismissed = ref(false)
const activePhase = ref<string | null>(null)

async function load() {
  try {
    const r = await fetch('/api/v1/quickstart')
    if (r.ok) wizard.value = await r.json()
  } catch { /* intentional: load failure leaves wizard empty */ }
}

async function markPhase(id: string, status: string) {
  await fetch(`/api/v1/quickstart/${id}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ status }),
  })
  activePhase.value = null
  await load()
}

async function dismiss() {
  dismissed.value = true
  await fetch('/api/v1/quickstart/dismiss', { method: 'POST' })
}

function phaseIcon(phase: any): string {
  if (phase.status === 'complete') return '✅'
  if (phase.status === 'skipped') return '⏭'
  const icons: Record<string, string> = {
    platform: '🔧', traefik: '🌐', auth: '🔐', tunnel: '🚇',
    storage: '📁', routing: '🎬', apps: '📦', llm: '🤖',
  }
  return icons[phase.id] ?? '⬜'
}

onMounted(load)
defineExpose({ load })
</script>
