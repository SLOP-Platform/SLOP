<template>
  <div class="p-4 max-w-4xl mx-auto w-full">
    <div class="flex items-center justify-between mb-6">
      <div>
        <h1 class="page-title">
          Storage
        </h1>
        <p class="page-subtitle">
          External NAS, cloud storage, and remote filesystem connections
        </p>
      </div>
    </div>

    <!-- Host disk summary from system fingerprint -->
    <div
      v-if="hostDisks.length"
      class="card mb-4"
    >
      <div class="card-body !py-2.5">
        <div class="flex items-center gap-2 mb-2">
          <span class="text-xs font-medium text-slate-600">Host storage</span>
          <span class="text-xs text-slate-400">· {{ hostServer }}</span>
        </div>
        <div class="space-y-1.5">
          <div
            v-for="disk in hostDisks"
            :key="disk.path"
            class="flex items-center gap-3"
          >
            <span class="text-xs font-mono text-slate-500 w-20 shrink-0 truncate">{{ disk.path }}</span>
            <div class="flex-1 h-1.5 bg-slate-100 rounded-full overflow-hidden">
              <div
                class="h-full rounded-full transition-all"
                :class="(disk.pct_used || disk.percent_used) > 90 ? 'bg-red-500' :
                  (disk.pct_used || disk.percent_used) > 80 ? 'bg-amber-400' : 'bg-sky-400'"
                :style="`width: ${disk.pct_used || disk.percent_used}%`"
              />
            </div>
            <span class="text-xs text-slate-500 shrink-0">{{ disk.free_gb }}GB free of {{ disk.total_gb }}GB</span>
            <span
              :class="['text-xs font-medium shrink-0',
                       (disk.pct_used || disk.percent_used) > 90 ? 'text-red-500' :
                       (disk.pct_used || disk.percent_used) > 80 ? 'text-amber-500' : 'text-slate-400']"
            >
              {{ disk.pct_used || disk.percent_used }}%
            </span>
          </div>
        </div>
      </div>
    </div>

    <div
      v-if="loadingInitial"
      class="space-y-2 animate-pulse"
    >
      <div
        v-for="n in 2"
        :key="n"
        class="card card-body h-14 bg-slate-50"
      />
    </div>
    <div
      v-else-if="sources.length === 0 && !showAdd"
      class="card card-body text-center py-12"
    >
      <div class="text-4xl mb-3">
        💾
      </div>
      <div class="font-semibold text-slate-700">
        No storage sources configured
      </div>
      <p class="text-sm text-slate-400 mt-2 max-w-sm mx-auto">
        Add an NFS share, SMB drive, or cloud storage to use as your media library location.
      </p>
      <button
        class="btn-primary btn-sm mt-4 inline-flex"
        @click="showAdd = true"
      >
        Add storage source
      </button>
    </div>

    <div
      v-if="sources.length > 0 && !showAdd"
      class="flex justify-end mb-3"
    >
      <button
        class="btn-secondary btn-sm"
        @click="showAdd = true"
      >
        + Add source
      </button>
    </div>
    <div
      v-else-if="sources.length > 0"
      class="mb-1"
    />
    <div
      v-if="sources.length > 0"
      class="space-y-3"
    >
      <div
        v-for="src in sources"
        :key="src.id ?? src.name"
        class="card card-body"
      >
        <div class="flex items-center justify-between">
          <div class="flex items-center gap-3">
            <div class="w-10 h-10 bg-slate-100 rounded-xl flex items-center justify-center text-xl">
              {{ typeIcon(src.source_type) }}
            </div>
            <div>
              <div class="font-medium text-slate-900 flex items-center gap-2">
                {{ src.name }}
                <span
                  v-if="src.is_primary"
                  class="badge badge-blue text-xs"
                >primary</span>
              </div>
              <div class="text-xs text-slate-400">
                {{ src.source_type.toUpperCase() }} · {{ src.mount_point }}
              </div>
              <div
                v-if="src.remote_host"
                class="text-xs text-slate-400"
              >
                {{ src.remote_host }}{{ src.remote_path }}
              </div>
            </div>
          </div>
          <div class="flex items-center gap-2">
            <span :class="['badge text-xs', src.status === 'active' ? 'badge-green' : src.status === 'error' ? 'badge-red' : 'badge-gray']">
              {{ src.status }}
            </span>
            <button
              class="btn-secondary btn-sm"
              @click="verify(src.id!)"
            >
              Verify
            </button>
            <button
              class="btn-ghost btn-sm"
              @click="getConfig(src.id!)"
            >
              Config
            </button>
          </div>
        </div>
        <div
          v-if="src.error_message"
          class="mt-2 text-xs text-red-600 bg-red-50 rounded px-2 py-1"
        >
          {{ src.error_message }}
        </div>
      </div>
    </div>

    <!-- Add storage — inline panel, same width as source cards -->
    <div
      v-if="showAdd"
      class="card mb-4"
    >
      <div class="card-header flex items-center justify-between">
        <span class="font-semibold text-sm">Add Storage Source</span>
        <button
          class="text-slate-400 hover:text-slate-600 text-sm"
          @click="showAdd = false; addError = null"
        >
          ✕
        </button>
      </div>
      <div class="card-body space-y-3">
        <div class="grid sm:grid-cols-2 gap-3">
          <div>
            <label class="label">Name</label>
            <input
              v-model="form.name"
              class="input"
              placeholder="Main NAS"
            >
          </div>
          <div>
            <label class="label">Type</label>
            <select
              v-model="form.source_type"
              class="input"
            >
              <option value="nfs">
                NFS (LAN NAS)
              </option>
              <option value="smb">
                SMB/CIFS (Synology, Windows)
              </option>
              <option value="rclone">
                rclone (S3, Backblaze, SFTP)
              </option>
              <option value="local">
                Local path (already mounted)
              </option>
            </select>
          </div>
          <div v-if="form.source_type !== 'local'">
            <label class="label">Remote host</label>
            <input
              v-model="form.remote_host"
              class="input"
              placeholder="10.0.1.100"
            >
          </div>
          <div v-if="['nfs','smb'].includes(form.source_type)">
            <label class="label">{{ form.source_type === 'nfs' ? 'Export path' : 'Share name' }}</label>
            <input
              v-model="form.remote_path"
              class="input"
              :placeholder="form.source_type === 'nfs' ? '/volume1/media' : 'media'"
            >
          </div>
          <div>
            <label class="label">Mount point</label>
            <input
              v-model="form.mount_point"
              class="input"
              placeholder="/mnt/nas01"
            >
          </div>
          <label class="flex items-center gap-2 cursor-pointer self-end pb-1">
            <input
              v-model="form.is_primary"
              type="checkbox"
              class="rounded border-slate-300"
            >
            <span class="text-sm text-slate-700">Primary media root</span>
          </label>
        </div>
        <div
          v-if="addError"
          class="text-sm text-red-600 bg-red-50 rounded p-2"
        >
          {{ addError }}
        </div>
        <div class="flex gap-3 pt-1">
          <button
            class="btn-secondary btn-sm"
            @click="showAdd = false; addError = null"
          >
            Cancel
          </button>
          <button
            :disabled="adding"
            class="btn-primary btn-sm"
            @click="addSource"
          >
            {{ adding ? 'Adding…' : 'Add source' }}
          </button>
        </div>
      </div>
    </div>

    <!-- Config modal -->
    <Teleport to="body">
      <div
        v-if="configData"
        class="fixed inset-0 z-50 flex items-center justify-center"
      >
        <div
          class="absolute inset-0 bg-black/30 backdrop-blur-sm"
          @click="configData = null"
        />
        <div class="relative card w-full max-w-xl mx-4">
          <div class="card-header flex items-center justify-between">
            <span class="font-semibold">Mount Configuration</span>
            <button
              class="text-slate-400 hover:text-slate-600"
              @click="configData = null"
            >
              ✕
            </button>
          </div>
          <div class="card-body space-y-4">
            <p class="text-sm text-slate-600">
              {{ configData.note }}
            </p>
            <div v-if="configData.systemd_unit">
              <div class="section-title mb-2">
                Systemd unit ({{ configData.systemd_unit_name }})
              </div>
              <pre class="bg-slate-950 text-slate-300 text-xs font-mono p-3 rounded-lg overflow-x-auto whitespace-pre-wrap">{{ configData.systemd_unit }}</pre>
            </div>
            <div v-if="configData.install_steps?.length">
              <div class="section-title mb-2">
                Install steps
              </div>
              <div class="space-y-1">
                <div
                  v-for="(step, i) in configData.install_steps"
                  :key="i"
                  class="flex items-start gap-2 text-sm"
                >
                  <span class="text-slate-400 font-mono text-xs mt-0.5 shrink-0">{{ String(i+1).padStart(2,'0') }}</span>
                  <code class="font-mono text-xs text-slate-700 bg-slate-50 px-1.5 py-0.5 rounded break-all">{{ step }}</code>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted } from 'vue'
const loadingInitial = ref(true)
import { useToast } from '@/composables/useToast'
import { storage } from '../api/client'
import type { StorageSource } from '../api/client'

const toast = useToast()
const sources = ref<StorageSource[]>([])
const hostDisks = ref<any[]>([])
const hostServer = ref('')
const showAdd = ref(false)
const adding = ref(false)
const addError = ref<string | null>(null)
const configData = ref<any>(null)
const form = ref({ name: '', source_type: 'nfs', remote_host: '', remote_path: '', mount_point: '', is_primary: false })
const TYPE_ICONS: Record<string, string> = { nfs: '🖥️', smb: '🪟', rclone: '☁️', local: '💾' }
const typeIcon = (t: string) => TYPE_ICONS[t] ?? '💾'

async function addSource() {
  adding.value = true; addError.value = null
  try {
    await storage.add(form.value as any)
    sources.value = await storage.list()
    showAdd.value = false
    form.value = { name: '', source_type: 'nfs', remote_host: '', remote_path: '', mount_point: '', is_primary: false }
  } catch (e) { addError.value = e instanceof Error ? e.message : String(e) }
  finally { adding.value = false }
}

async function verify(id: number) {
  try { await storage.verify(id); sources.value = await storage.list() } catch (e) { toast.error('Verify failed.', String(e)) }
}

async function getConfig(id: number) {
  try { configData.value = await storage.config(id) } catch (e) { toast.error('Could not load config.', String(e)) }
}

onMounted(async () => { try { sources.value = await storage.list() } catch (e) { toast.error('Could not load storage sources.', String(e)) } finally { loadingInitial.value = false } })
</script>
