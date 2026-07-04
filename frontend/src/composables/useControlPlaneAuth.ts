import { ref, computed } from 'vue'
import { controlPlane, type ControlPlanePosture } from '@/api/client'

// Consumes GET /api/v1/control-plane/posture (#1250 / #976 Phase-C). Backs the
// Settings posture badge — the no-phantom-owner freshness signal (DToC judge C6):
// the default-`off` control-plane auth feature must surface a RED posture so it
// cannot silently rot inert. Mirrors useAdvisories/useAgentIntegrity: the fetch is
// best-effort; a failure leaves the posture null rather than surfacing a fatal error.
export function useControlPlaneAuth() {
  const posture = ref<ControlPlanePosture | null>(null)

  async function fetchPosture(): Promise<void> {
    try {
      posture.value = await controlPlane.posture()
    } catch {
      /* intentional: a read-surface failure is non-fatal — render unknown */
    }
  }

  // Badge colour: the backend already derives RED-dominance (mode=off OR
  // unprovisioned ⇒ red); a null posture (fetch failed) renders neutral/unknown.
  const badgeColor = computed((): 'red' | 'amber' | 'green' | 'unknown' => {
    const p = posture.value?.posture
    if (p === 'red' || p === 'amber' || p === 'green') return p
    return 'unknown'
  })

  const badgeLabel = computed((): string => {
    const p = posture.value
    if (!p) return 'Control plane: unknown'
    if (!p.token_provisioned) return 'Control plane: RED — no token provisioned'
    if (p.mode === 'off') return 'Control plane: RED — auth off (inert)'
    if (p.mode === 'observe') return `Control plane: AMBER — observe (${p.observe_would_reject_count} would-reject)`
    return 'Control plane: GREEN — enforcing'
  })

  const isInert = computed((): boolean => badgeColor.value === 'red')

  return { posture, fetchPosture, badgeColor, badgeLabel, isInert }
}
