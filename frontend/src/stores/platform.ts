// src/stores/platform.ts
import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { platform as platformApi, type PlatformStatus } from '../api/client'

export const usePlatformStore = defineStore('platform', () => {
  const status = ref<PlatformStatus | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  const isReady = computed(() => status.value?.status === 'ready')
  const domain = computed(() => status.value?.domain ?? null)

  async function fetchStatus() {
    loading.value = true
    error.value = null
    try {
      status.value = await platformApi.status()
    } catch (e) {
      error.value = e instanceof Error ? e.message : String(e)
    } finally {
      loading.value = false
    }
  }

  function clearStatus() {
    // Optimistically clear status so sidebar updates instantly on reset click
    // (fetchStatus will re-populate with real data from the server)
    status.value = null
  }

  return { status, loading, error, isReady, domain, fetchStatus, clearStatus }
})
