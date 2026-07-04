<template>
  <div
    v-if="!dismissed"
    class="rounded-lg border border-amber-200 bg-amber-50 p-4 shadow-sm"
  >
    <!-- Header row -->
    <div class="flex items-center justify-between mb-2">
      <div class="flex items-center gap-2 text-sm font-semibold text-amber-800">
        <span>🔬 Diagnosis</span>
        <span class="text-amber-400">·</span>
        <span>{{ appKey }}</span>
        <span class="text-amber-400">·</span>
        <span class="font-mono text-xs bg-amber-100 px-1.5 py-0.5 rounded text-amber-700">{{ diagnosisClass }}</span>
      </div>
      <span class="text-xs text-amber-500">{{ formattedDate }}</span>
    </div>

    <!-- Confidence bar -->
    <div class="flex items-center gap-2 mb-3">
      <span class="text-xs text-amber-700 shrink-0">Confidence:</span>
      <div class="flex-1 max-w-32 h-1.5 bg-amber-200 rounded-full overflow-hidden">
        <div
          class="h-full rounded-full transition-all duration-300"
          :class="confidenceColor"
          :style="{ width: confidencePct + '%' }"
        />
      </div>
      <span class="text-xs text-amber-700 shrink-0 font-medium">{{ confidencePct }}%</span>
    </div>

    <!-- Problem -->
    <p class="text-sm text-slate-700 mb-2">
      {{ problem }}
    </p>

    <!-- Suggested fix -->
    <div class="mb-3">
      <p class="text-xs font-semibold text-amber-800 mb-1">
        Suggested fix:
      </p>
      <p class="text-sm text-slate-700 whitespace-pre-wrap font-mono text-xs leading-relaxed bg-white rounded border border-amber-100 px-3 py-2">
        {{ suggestedFix }}
      </p>
    </div>

    <!-- Amber banner for 501 response -->
    <div
      v-if="applyBanner"
      class="text-xs bg-amber-100 border border-amber-200 rounded px-3 py-2 text-amber-800 mb-3"
    >
      {{ applyBanner }}
    </div>

    <!-- Actions -->
    <div class="flex items-center gap-2">
      <button
        :disabled="applying"
        class="text-xs px-3 py-1 rounded border border-amber-300 bg-white text-amber-800 hover:bg-amber-100 transition-colors disabled:opacity-50"
        @click="handleApply"
      >
        <span
          v-if="applying"
          class="flex items-center gap-1"
        >
          <span class="inline-block w-2.5 h-2.5 border border-amber-400 border-t-transparent rounded-full animate-spin" />
          Applying…
        </span>
        <span v-else>Apply fix ▶</span>
      </button>
      <button
        class="text-xs px-3 py-1 rounded border border-amber-200 text-amber-600 hover:bg-amber-100 transition-colors"
        @click="handleDismiss"
      >
        Dismiss
      </button>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed } from 'vue'

interface DiagnosisCardProps {
  id: number
  appKey: string
  problem: string
  diagnosisClass: string
  suggestedFix: string
  confidence: number   // 0.0–1.0
  status: string
  createdAt: number    // Unix timestamp
}

const props = defineProps<DiagnosisCardProps>()
const emit = defineEmits<{
  apply: [id: number]
  dismiss: [id: number]
}>()

const dismissed = ref(false)
const applying = ref(false)
const applyBanner = ref('')

const confidencePct = computed(() => Math.round(props.confidence * 100))

const confidenceColor = computed(() => {
  if (props.confidence >= 0.8) return 'bg-green-500'
  if (props.confidence >= 0.5) return 'bg-amber-500'
  return 'bg-red-400'
})

const formattedDate = computed(() => {
  const d = new Date(props.createdAt * 1000)
  return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })
})

async function handleApply() {
  applying.value = true
  applyBanner.value = ''
  try {
    const res = await fetch(`/api/v1/agent/fixes/${props.id}/apply`, { method: 'POST' })
    if (res.status === 501) {
      applyBanner.value = 'Auto-apply not yet available — this feature is coming in the next release.'
    } else if (!res.ok) {
      applyBanner.value = 'Could not apply fix — please try again later.'
    } else {
      emit('apply', props.id)
    }
  } catch {
    applyBanner.value = 'Network error — could not reach the server.'
  } finally {
    applying.value = false
  }
}

function handleDismiss() {
  dismissed.value = true
  emit('dismiss', props.id)
}
</script>
