import { ref, computed } from 'vue'
import type { IntegrityStatus } from '@/api/client'
import { health } from '@/api/client'

export function useAgentIntegrity() {
  const integrityStatus = ref<IntegrityStatus | null>(null)

  async function fetchIntegrity(): Promise<void> {
    try {
      integrityStatus.value = await health.integrity()
    } catch { /* intentional: integrity check failure is non-fatal */ }
  }

  const integrityLabel = computed((): string => {
    const s = integrityStatus.value
    if (!s || s.status === 'unknown') return 'Unknown'
    if (s.critical_gaps > 0) return `${s.critical_gaps} critical gap${s.critical_gaps > 1 ? 's' : ''}`
    if (s.high_gaps > 0) return `${s.high_gaps} high-risk gap${s.high_gaps > 1 ? 's' : ''}`
    return 'All rules covered'
  })

  const integrityColor = computed((): string => {
    const s = integrityStatus.value
    if (!s || s.status === 'unknown') return 'text-slate-400'
    if (s.status === 'critical') return 'text-red-500'
    if (s.status === 'degraded') return 'text-yellow-500'
    return 'text-green-600'
  })

  return { integrityStatus, fetchIntegrity, integrityLabel, integrityColor }
}
