// frontend/tests/components/ToastContainer.spec.ts
//
// Vitest mount test — proves the Vue Test Utils integration in the
// vitest infrastructure works end-to-end. Mounts ToastContainer.vue,
// drives state through the `useToast` composable (the same module
// the production app uses), and asserts the rendered DOM reflects
// the state.
//
// Companion to the unit-test-only seeds in tests/api/ +
// tests/composables/. This is the first real component-mount test
// in the suite — proves jsdom + @vue/test-utils + the @/ alias all
// work for SFC-level testing.
import { describe, it, expect, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import ToastContainer from '@/components/ToastContainer.vue'
import { useToast } from '@/composables/useToast'


describe('ToastContainer.vue (mount)', () => {
  beforeEach(() => {
    // Module-level toast state is shared — drain it between tests.
    const { toasts, dismiss } = useToast()
    while (toasts.length) dismiss(toasts[0].id)
  })

  it('mounts cleanly with an empty toast queue', () => {
    const w = mount(ToastContainer)
    expect(w.exists()).toBe(true)
    // No alert nodes when there are no toasts.
    expect(w.findAll('[role="alert"]').length).toBe(0)
  })

  it('renders one alert per pushed toast', async () => {
    const w = mount(ToastContainer)
    const toast = useToast()
    toast.success('Installed sonarr')
    toast.error('Cannot reach docker', 'Check the daemon')
    await w.vm.$nextTick()
    const alerts = w.findAll('[role="alert"]')
    expect(alerts.length).toBe(2)
  })

  it('renders the message and detail of an error toast', async () => {
    const w = mount(ToastContainer)
    const toast = useToast()
    toast.error('Connection refused', 'Check the firewall')
    await w.vm.$nextTick()
    const alert = w.find('[role="alert"]')
    expect(alert.text()).toContain('Connection refused')
    expect(alert.text()).toContain('Check the firewall')
  })

  it('removes the alert when the dismiss button is clicked', async () => {
    const w = mount(ToastContainer)
    const toast = useToast()
    toast.info('Syncing registry')
    await w.vm.$nextTick()
    expect(w.findAll('[role="alert"]').length).toBe(1)

    // The aria-labeled dismiss button is the per-toast ✕.
    await w.find('button[aria-label="Dismiss"]').trigger('click')
    await w.vm.$nextTick()
    expect(w.findAll('[role="alert"]').length).toBe(0)
  })

  it('applies the type-specific background class', async () => {
    const w = mount(ToastContainer)
    const toast = useToast()
    toast.success('It worked')
    await w.vm.$nextTick()
    // The BG map uses 'bg-green-50' for success — assert that class
    // lands on the rendered alert.
    const alert = w.find('[role="alert"]')
    expect(alert.classes()).toContain('bg-green-50')
    expect(alert.classes()).toContain('border-green-200')
  })
})
