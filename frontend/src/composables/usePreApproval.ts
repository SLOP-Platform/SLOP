import { ref } from 'vue'
import { settings } from '@/api/client'
import { useToast } from '@/composables/useToast'

export interface PreApprovalTier {
  tier: number
  name: string
  pre_approvable: boolean
  global_pre_approved: boolean
}

export interface PreApprovalView {
  tiers: PreApprovalTier[]
  per_app: Record<string, Record<string, boolean>>
  note?: string
}

// Backs the Settings "Pre-Approval Policy" panel (#1070 / operational plan §W5).
// The backend PreApprovalPolicy is a tier × scope (per-app) matrix; this composable
// wires the FULL read+write surface — global per-tier defaults AND per-app overrides
// — so the "scope" axis is editable from the UI, not API-only (the gap that kept
// #1070 open). Every mutating endpoint returns the fresh effective view, so each
// handler simply assigns the response. T3 (irreversible) is never offered for
// pre-approval — the backend refuses it (safety invariant 8); the UI omits it too.
export function usePreApproval() {
  const toast = useToast()
  const preApproval = ref<PreApprovalView | null>(null)
  const appOverrideForm = ref<{ app_key: string; tier: number; pre_approved: boolean }>({
    app_key: '',
    tier: 1,
    pre_approved: true,
  })

  async function loadPreApproval(): Promise<void> {
    try {
      preApproval.value = await settings.preapproval()
    } catch {
      toast.error('Could not load pre-approval policy.')
    }
  }

  async function setTierDefault(tier: number, preApproved: boolean): Promise<void> {
    try {
      preApproval.value = await settings.updateTier({ tier, pre_approved: preApproved })
      if (preApproved) {
        toast.warn(`Tier T${tier} pre-approved — the agent may act on these without asking.`)
      }
    } catch (e) {
      toast.error((e as Error).message || 'Could not update tier policy.')
    }
  }

  async function setAppOverride(): Promise<void> {
    const app = appOverrideForm.value.app_key.trim()
    if (!app) return
    try {
      preApproval.value = await settings.updateApp({
        app_key: app,
        tier: appOverrideForm.value.tier,
        pre_approved: appOverrideForm.value.pre_approved,
      })
      if (appOverrideForm.value.pre_approved) {
        toast.warn(`${app} T${appOverrideForm.value.tier} pre-approved — the agent may act on ${app} without asking.`)
      }
      appOverrideForm.value.app_key = ''
    } catch (e) {
      toast.error((e as Error).message || 'Could not set per-app override.')
    }
  }

  async function clearAppOverride(appKey: string): Promise<void> {
    try {
      preApproval.value = await settings.clearApp(appKey)
    } catch (e) {
      toast.error((e as Error).message || 'Could not clear per-app override.')
    }
  }

  return {
    preApproval,
    appOverrideForm,
    loadPreApproval,
    setTierDefault,
    setAppOverride,
    clearAppOverride,
  }
}
