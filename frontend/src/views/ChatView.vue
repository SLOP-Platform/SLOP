<template>
  <div class="max-w-3xl mx-auto p-4">
    <div class="card">
      <div class="card-header">
        <div class="font-semibold text-sm">
          Agent Chat
        </div>
        <div class="text-xs text-slate-400 mt-0.5">
          Ask for status or tell the agent to act. Actions that aren't pre-approved
          ask you to confirm first.
        </div>
      </div>

      <div
        ref="logEl"
        class="card-body space-y-3 max-h-[60vh] overflow-y-auto"
      >
        <div
          v-for="(m, i) in messages"
          :key="i"
          :class="['flex', m.role === 'user' ? 'justify-end' : 'justify-start']"
        >
          <div
            :class="['rounded-lg px-3 py-2 text-sm whitespace-pre-wrap max-w-[80%]',
                     m.role === 'user' ? 'bg-sky-500 text-white'
                     : m.kind === 'denied' ? 'bg-red-50 text-red-700 border border-red-100'
                       : m.kind === 'needs_approval' ? 'bg-amber-50 text-amber-800 border border-amber-100'
                         : m.kind === 'acted' ? 'bg-emerald-50 text-emerald-800 border border-emerald-100'
                           : 'bg-slate-100 text-slate-700']"
          >
            {{ m.text }}
            <div
              v-if="m.role === 'agent' && m.kind === 'needs_approval' && m.pending"
              class="mt-2 flex gap-2"
            >
              <button
                class="btn-primary btn-sm"
                @click="confirmPending(m)"
              >
                Do it
              </button>
              <button
                class="btn-secondary btn-sm"
                @click="m.pending = null"
              >
                Cancel
              </button>
            </div>
          </div>
        </div>
      </div>

      <div class="card-body border-t border-slate-100 flex gap-2">
        <input
          v-model="draft"
          type="text"
          placeholder="e.g. restart sonarr, or: how is plex?"
          class="input flex-1"
          :disabled="busy"
          @keyup.enter="send()"
        >
        <button
          class="btn-primary"
          :disabled="busy || !draft.trim()"
          @click="send()"
        >
          Send
        </button>
      </div>
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, nextTick } from 'vue'
import { chat as chatApi } from '../api/client'

interface ChatMsg {
  role: 'user' | 'agent'
  text: string
  kind?: string
  // For needs_approval: the original action context + token to echo back.
  pending?: { action_id: string; app_key: string; approval_token: string } | null
}

const messages = ref<ChatMsg[]>([])
const draft = ref('')
const busy = ref(false)
const logEl = ref<HTMLElement | null>(null)

async function scrollDown() {
  await nextTick()
  if (logEl.value) logEl.value.scrollTop = logEl.value.scrollHeight
}

// The original natural-language message keyed by pending token, so a confirm
// re-sends the SAME action intent (the backend re-classifies it and validates the
// single-use token bound to that action/app).
const pendingText = ref<Record<string, string>>({})

async function postChat(message: string, approvalToken?: string, appKey?: string) {
  const body: Record<string, unknown> = { message }
  if (approvalToken) body.approval_token = approvalToken
  if (appKey) body.app_key = appKey
  const res = await chatApi.post(body)
  if (res.status === 401 || res.status === 403) {
    return { reply: 'The control plane is locked — set a control-plane token to act.', kind: 'denied' }
  }
  return res.data
}

async function send(rawMessage?: string, approvalToken?: string, appKey?: string) {
  const message = (rawMessage ?? draft.value).trim()
  if (!message) return
  if (!rawMessage) {
    messages.value.push({ role: 'user', text: message })
    draft.value = ''
  }
  busy.value = true
  await scrollDown()
  try {
    const data = await postChat(message, approvalToken, appKey)
    const msg: ChatMsg = { role: 'agent', text: data.reply, kind: data.kind }
    if (data.kind === 'needs_approval' && data.approval_token) {
      msg.pending = {
        action_id: data.action_id,
        app_key: data.app_key,
        approval_token: data.approval_token,
      }
      pendingText.value[data.approval_token] = message
    }
    messages.value.push(msg)
  } catch {
    messages.value.push({ role: 'agent', text: 'Something went wrong reaching the agent.', kind: 'denied' })
  } finally {
    busy.value = false
    await scrollDown()
  }
}

async function confirmPending(m: ChatMsg) {
  if (!m.pending) return
  const { approval_token, app_key } = m.pending
  const original = pendingText.value[approval_token] || ''
  m.pending = null // single-use UI: clear the buttons immediately
  messages.value.push({ role: 'user', text: 'Do it' })
  await send(original, approval_token, app_key)
}
</script>
