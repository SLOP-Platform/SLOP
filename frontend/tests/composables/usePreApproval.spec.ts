// frontend/tests/composables/usePreApproval.spec.ts
//
// Vitest unit test for the usePreApproval composable (#1070 — tier × scope
// pre-approval policy). Verifies the FULL write surface the UI exposes: load,
// global per-tier default, and the per-app override SET/CLEAR (the scope axis
// that was previously API-only). Asserts each handler hits the canonical
// endpoint with the right method/body and assigns the returned effective view.
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { defineComponent, ref } from 'vue'

interface Call {
  url: string
  method: string
  body: unknown
}

let calls: Call[] = []
let nextResponse: Response = new Response('{}', { status: 200 })

beforeEach(() => {
  calls = []
  nextResponse = new Response('{}', { status: 200 })
  globalThis.fetch = vi.fn((url: string | URL | Request, init?: RequestInit) => {
    calls.push({
      url: typeof url === 'string' ? url : (url as Request).url,
      method: (init?.method || 'GET').toUpperCase(),
      body: init?.body ? JSON.parse(init.body as string) : undefined,
    })
    return Promise.resolve(nextResponse)
  }) as unknown as typeof fetch
})

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const VIEW = {
  tiers: [{ tier: 1, name: 'REVERSIBLE', pre_approvable: true, global_pre_approved: false }],
  per_app: { plex: { '1': true } },
  note: 'T3 can never be pre-approved.',
}

describe('usePreApproval', () => {
  it('loadPreApproval GETs the canonical policy surface and populates state', async () => {
    const { usePreApproval } = await import('@/composables/usePreApproval')
    nextResponse = jsonResponse(VIEW)
    const { preApproval, loadPreApproval } = usePreApproval()
    await loadPreApproval()
    expect(calls[0].url).toMatch(/\/settings\/preapproval$/)
    expect(calls[0].method).toBe('GET')
    expect(preApproval.value?.per_app.plex['1']).toBe(true)
  })

  it('setTierDefault PUTs the tier endpoint with the body', async () => {
    const { usePreApproval } = await import('@/composables/usePreApproval')
    nextResponse = jsonResponse(VIEW)
    const { setTierDefault } = usePreApproval()
    await setTierDefault(1, true)
    expect(calls[0].url).toMatch(/\/settings\/preapproval\/tier$/)
    expect(calls[0].method).toBe('PUT')
    expect(calls[0].body).toEqual({ tier: 1, pre_approved: true })
  })

  it('setAppOverride PUTs the app endpoint and clears the form key on success', async () => {
    const { usePreApproval } = await import('@/composables/usePreApproval')
    nextResponse = jsonResponse(VIEW)
    const { appOverrideForm, setAppOverride } = usePreApproval()
    appOverrideForm.value = { app_key: ' plex ', tier: 2, pre_approved: false }
    await setAppOverride()
    expect(calls[0].url).toMatch(/\/settings\/preapproval\/app$/)
    expect(calls[0].method).toBe('PUT')
    expect(calls[0].body).toEqual({ app_key: 'plex', tier: 2, pre_approved: false })
    expect(appOverrideForm.value.app_key).toBe('')
  })

  it('setAppOverride is a no-op when the app key is blank (fail-closed, no request)', async () => {
    const { usePreApproval } = await import('@/composables/usePreApproval')
    const { appOverrideForm, setAppOverride } = usePreApproval()
    appOverrideForm.value = { app_key: '   ', tier: 1, pre_approved: true }
    await setAppOverride()
    expect(calls.length).toBe(0)
  })

  // GROUND the per-app pre-approve toggle: the SettingsView select binds a boolean
  // via `<option :value="true/false">`. A bound (v-bind) value preserves the real JS
  // type — unlike a plain `value="true"` attribute, which would coerce to a string and
  // send `"pre_approved": "true"` to the bool backend. This proves the binding type in
  // THIS toolchain (goes red if Vue ever changed select v-model coercion).
  it('a select v-model with :value boolean options binds a REAL boolean, not a string', async () => {
    const Comp = defineComponent({
      setup() {
        return { v: ref<boolean>(true) }
      },
      template:
        '<select v-model="v"><option :value="true">Pre-approve</option><option :value="false">Ask first</option></select>',
    })
    const wrapper = mount(Comp)
    await wrapper.findAll('option')[1].setValue()
    expect(wrapper.vm.v).toBe(false)
    expect(typeof wrapper.vm.v).toBe('boolean')
  })

  it('clearAppOverride DELETEs the per-app endpoint', async () => {
    const { usePreApproval } = await import('@/composables/usePreApproval')
    nextResponse = jsonResponse({ ...VIEW, per_app: {} })
    const { preApproval, clearAppOverride } = usePreApproval()
    await clearAppOverride('plex')
    expect(calls[0].url).toMatch(/\/settings\/preapproval\/app\/plex$/)
    expect(calls[0].method).toBe('DELETE')
    expect(preApproval.value?.per_app).toEqual({})
  })
})
