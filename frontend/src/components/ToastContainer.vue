<template>
  <!-- Fixed bottom-right toast stack, z-index above modals -->
  <div class="fixed bottom-5 right-5 z-[60] flex flex-col-reverse gap-2 w-80 pointer-events-none">
    <TransitionGroup
      name="toast"
      tag="div"
      class="flex flex-col-reverse gap-2"
    >
      <div
        v-for="toast in toasts"
        :key="toast.id"
        :class="[
          'flex items-start gap-3 px-4 py-3 rounded-xl shadow-lg border',
          'pointer-events-auto cursor-default select-none',
          BG[toast.type],
        ]"
        role="alert"
        @click="dismiss(toast.id)"
      >
        <!-- Icon -->
        <span class="text-base shrink-0 mt-0.5">{{ ICON[toast.type] }}</span>

        <!-- Content -->
        <div class="flex-1 min-w-0">
          <p :class="['text-sm font-medium leading-snug', TEXT[toast.type]]">
            {{ toast.message }}
          </p>
          <p
            v-if="toast.detail"
            :class="['text-xs mt-0.5 leading-snug opacity-80', TEXT[toast.type]]"
          >
            {{ toast.detail }}
          </p>
        </div>

        <!-- Dismiss button -->
        <button
          :class="['shrink-0 opacity-50 hover:opacity-100 transition-opacity text-sm', TEXT[toast.type]]"
          aria-label="Dismiss"
          @click.stop="dismiss(toast.id)"
        >
          ✕
        </button>
      </div>
    </TransitionGroup>
  </div>
</template>

<script setup lang="ts">
import { useToast } from '@/composables/useToast'

const { toasts, dismiss } = useToast()

const BG: Record<string, string> = {
  success: 'bg-green-50 border-green-200',
  error:   'bg-red-50 border-red-200',
  warning: 'bg-amber-50 border-amber-200',
  info:    'bg-sky-50 border-sky-200',
}

const TEXT: Record<string, string> = {
  success: 'text-green-800',
  error:   'text-red-800',
  warning: 'text-amber-800',
  info:    'text-sky-800',
}

const ICON: Record<string, string> = {
  success: '✓',
  error:   '✗',
  warning: '!',
  info:    'ℹ',
}
</script>

<style scoped>
.toast-enter-active {
  transition: all 0.22s cubic-bezier(0.34, 1.56, 0.64, 1);
}
.toast-leave-active {
  transition: all 0.18s ease-in;
}
.toast-enter-from {
  opacity: 0;
  transform: translateX(40px) scale(0.95);
}
.toast-leave-to {
  opacity: 0;
  transform: translateX(40px) scale(0.95);
}
</style>
