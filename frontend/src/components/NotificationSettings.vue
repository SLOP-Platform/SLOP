<script setup lang="ts">
// Notification settings block (#1147) — extracted from SettingsView so the view
// stays under its size cap and gains the notifier-provider + gotify fields here.
// Two-way bound to the parent's form fields via v-model (no prop mutation); the
// parent persists them via PUT /api/settings.
const ntfyEnabled = defineModel<boolean>('ntfyEnabled', { required: true })
const ntfyUrl = defineModel<string>('ntfyUrl', { required: true })
const ntfyTopic = defineModel<string>('ntfyTopic', { required: true })
const provider = defineModel<string>('provider', { required: true })
const gotifyUrl = defineModel<string>('gotifyUrl', { required: true })
const gotifyToken = defineModel<string>('gotifyToken', { required: true })
</script>

<template>
  <div class="space-y-3">
    <!-- ntfy alerts row -->
    <div class="flex items-center gap-3 border-t border-slate-100 pt-3">
      <label class="flex items-center gap-2 w-28 shrink-0 cursor-pointer">
        <input
          v-model="ntfyEnabled"
          type="checkbox"
          class="w-3.5 h-3.5 rounded border-slate-300"
        >
        <span class="text-xs font-medium text-slate-600">ntfy alerts</span>
      </label>
      <input
        v-model="ntfyUrl"
        class="input text-xs flex-1"
        placeholder="http://ntfy:80"
        :disabled="!ntfyEnabled"
      >
      <input
        v-model="ntfyTopic"
        class="input text-xs w-28"
        placeholder="slop"
        :disabled="!ntfyEnabled"
      >
    </div>
    <!-- notifier provider row -->
    <div class="flex items-center gap-3">
      <span class="text-xs font-medium text-slate-600 w-28 shrink-0">Deliver via</span>
      <select
        v-model="provider"
        class="input text-xs w-40"
      >
        <option value="ntfy">
          ntfy
        </option>
        <option value="gotify">
          Gotify
        </option>
      </select>
      <span class="text-xs text-slate-400">Unknown providers are rejected (fail-closed)</span>
    </div>
    <!-- gotify fields (only when selected) -->
    <div
      v-if="provider === 'gotify'"
      class="flex items-center gap-3"
    >
      <span class="text-xs font-medium text-slate-600 w-28 shrink-0">Gotify</span>
      <input
        v-model="gotifyUrl"
        class="input text-xs flex-1"
        placeholder="http://gotify:80"
      >
      <input
        v-model="gotifyToken"
        type="password"
        class="input text-xs w-40"
        placeholder="app token"
        autocomplete="off"
      >
    </div>
  </div>
</template>
