import { ref, computed } from 'vue'
import type { SpineAdvisory } from '@/api/client'
import { health } from '@/api/client'

// Consumes GET /api/v1/health/advisories (#1089 read surface) — the store-only
// spine advisories feed (advisory LLM annotations persisted alongside GROUND
// verdicts for human review). Mirrors useAgentIntegrity: fetch is best-effort,
// a failure leaves the list empty rather than surfacing a fatal error.
export function useAdvisories() {
  const advisories = ref<SpineAdvisory[]>([])

  async function fetchAdvisories(limit = 100): Promise<void> {
    try {
      const r = await health.advisories(limit)
      advisories.value = r.advisories ?? []
    } catch { /* intentional: a read-surface failure is non-fatal — show nothing */ }
  }

  const advisoryCount = computed((): number => advisories.value.length)

  const hasAdvisories = computed((): boolean => advisories.value.length > 0)

  // A one-line annotation preview for an advisory: prefer a `summary`/`note`
  // field on the parsed object, else stringify, else the raw text.
  function annotationText(a: SpineAdvisory): string {
    const ann = a.annotation
    if (ann == null) return ''
    if (typeof ann === 'string') return ann
    const obj = ann as Record<string, unknown>
    const preferred = obj.summary ?? obj.note ?? obj.message
    if (typeof preferred === 'string') return preferred
    try { return JSON.stringify(ann) } catch { return '' }
  }

  return { advisories, fetchAdvisories, advisoryCount, hasAdvisories, annotationText }
}
