// src/composables/useToast.ts
// Global toast notification system.
// Usage:
//   import { useToast } from '@/composables/useToast'
//   const toast = useToast()
//   toast.success('App installed.')
//   toast.error('Could not connect.', 'Check your network settings.')
//   toast.info('Syncing registry…')
//   toast.warn('CF credentials not configured.')

import { reactive } from 'vue'

export type ToastType = 'success' | 'error' | 'warning' | 'info'

export interface Toast {
  id: number
  type: ToastType
  message: string
  detail?: string
  duration: number   // ms — 0 = sticky (must be dismissed manually)
  _timer?: ReturnType<typeof setTimeout>
}

// Module-level reactive state — single instance shared across all components
const state = reactive<{ toasts: Toast[] }>({ toasts: [] })
let _nextId = 1

function show(
  type: ToastType,
  message: string,
  detail?: string,
  duration = 4000,
): number {
  const id = _nextId++
  const toast: Toast = { id, type, message, detail, duration }

  state.toasts.push(toast)

  if (duration > 0) {
    toast._timer = setTimeout(() => dismiss(id), duration)
  }

  // Keep at most 5 toasts visible — auto-dismiss oldest if overflow
  if (state.toasts.length > 5) {
    const oldest = state.toasts[0]
    if (oldest._timer) clearTimeout(oldest._timer)
    state.toasts.shift()
  }

  return id
}

function dismiss(id: number) {
  const idx = state.toasts.findIndex(t => t.id === id)
  if (idx !== -1) {
    const toast = state.toasts[idx]
    if (toast._timer) clearTimeout(toast._timer)
    state.toasts.splice(idx, 1)
  }
}

function dismissAll() {
  state.toasts.forEach(t => { if (t._timer) clearTimeout(t._timer) })
  state.toasts.splice(0)
}

export function useToast() {
  return {
    toasts: state.toasts,

    /** Green — operation succeeded */
    success: (message: string, detail?: string, duration = 4000) =>
      show('success', message, detail, duration),

    /** Red — operation failed */
    error: (message: string, detail?: string, duration = 6000) =>
      show('error', message, detail, duration),

    /** Amber — non-fatal warning */
    warn: (message: string, detail?: string, duration = 5000) =>
      show('warning', message, detail, duration),

    /** Blue — neutral information */
    info: (message: string, detail?: string, duration = 3500) =>
      show('info', message, detail, duration),

    dismiss,
    dismissAll,
  }
}
