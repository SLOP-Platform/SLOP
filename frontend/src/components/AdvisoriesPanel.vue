<template>
  <!-- Spine advisories — store-only LLM annotations alongside GROUND verdicts,
       surfaced for human review (#1213; consumes #1089 GET /health/advisories).
       Hidden entirely when the feed is empty so it never adds noise. -->
  <div v-if="hasAdvisories" class="card card-body !py-3 border-l-2 border-l-indigo-300 mb-2">
    <div class="flex items-center justify-between mb-1">
      <div class="flex items-center gap-2">
        <span class="text-base">📝</span>
        <span class="text-sm font-medium text-slate-800">Advisories</span>
      </div>
      <span class="text-xs font-medium text-slate-400">{{ advisoryCount }} pending review</span>
    </div>
    <ul class="space-y-1">
      <li
        v-for="a in advisories"
        :key="a.id"
        class="flex items-start justify-between gap-2 text-xs"
      >
        <div class="min-w-0">
          <span :class="['font-medium', verdictColor(a.verdict)]">{{ a.verdict }}</span>
          <span class="text-slate-600 ml-1">{{ a.finding_id }}</span>
          <p v-if="annotationText(a)" class="text-slate-400 truncate">
            {{ annotationText(a) }}
          </p>
        </div>
        <span class="text-slate-300 shrink-0" :title="`${a.provider}`">
          {{ formatAgeTimestamp(a.created_at) }}
        </span>
      </li>
    </ul>
  </div>
</template>

<script setup lang="ts">
import { onMounted } from 'vue'
import { useAdvisories } from '@/composables/useAdvisories'
import { useHealthFormatters } from '@/composables/useHealthFormatters'

const { advisories, fetchAdvisories, advisoryCount, hasAdvisories, annotationText } = useAdvisories()
const { formatAgeTimestamp } = useHealthFormatters()

function verdictColor(verdict: string): string {
  if (verdict === 'drift' || verdict === 'DRIFT') return 'text-red-500'
  if (verdict === 'inconsistent' || verdict === 'INCONSISTENT') return 'text-amber-500'
  if (verdict === 'verified') return 'text-green-600'
  return 'text-slate-500'
}

onMounted(fetchAdvisories)
</script>
