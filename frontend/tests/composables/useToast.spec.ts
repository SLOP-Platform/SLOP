// frontend/tests/composables/useToast.spec.ts
//
// Vitest seed test for a Vue composable. `useToast` is the global
// notification system; tests here verify the public API contract
// without mounting a Vue component (purer unit-test surface for
// the seed test).
//
// A higher-level component test that mounts `ToastContainer.vue`
// is the natural follow-up — left for a subsequent commit.
import { describe, it, expect, beforeEach } from 'vitest'
import { useToast } from '@/composables/useToast'

describe('useToast', () => {
  beforeEach(() => {
    // Clear any leftover toasts between tests — module-level state.
    const { toasts, dismiss } = useToast()
    while (toasts.length) dismiss(toasts[0].id)
  })

  it('starts with an empty toast queue', () => {
    const { toasts } = useToast()
    expect(toasts.length).toBe(0)
  })

  it('success() pushes a success-typed toast', () => {
    const t = useToast()
    t.success('Installed')
    expect(t.toasts.length).toBe(1)
    expect(t.toasts[0].type).toBe('success')
    expect(t.toasts[0].message).toBe('Installed')
  })

  it('error() pushes an error-typed toast with optional detail', () => {
    const t = useToast()
    t.error('Connection refused', 'Check the firewall')
    expect(t.toasts[0].type).toBe('error')
    expect(t.toasts[0].detail).toBe('Check the firewall')
  })

  it('dismiss() removes the toast by id', () => {
    const t = useToast()
    t.info('hello')
    const id = t.toasts[0].id
    t.dismiss(id)
    expect(t.toasts.length).toBe(0)
  })

  it('assigns unique ids across pushes', () => {
    const t = useToast()
    t.info('one')
    t.info('two')
    t.info('three')
    const ids = t.toasts.map(x => x.id)
    expect(new Set(ids).size).toBe(3)
  })
})
