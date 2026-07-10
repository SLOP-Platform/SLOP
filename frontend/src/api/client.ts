/// <reference lib="dom" />
// src/api/client.ts
// Typed API client for the SLOP backend.
//
// Step 3.2.e: BASE updated from `/api` to `/api/v1` per ADR 0005
// (URL-path API versioning). Every call routed through this module
// now hits the versioned mount. Raw `fetch('/api/...')` calls in
// individual view files continue to work via the dual-mount on the
// backend (legacy `/api/<area>` is a deprecated alias) — those will
// migrate incrementally as their files are touched.

const BASE = '/api/v1'

async function request<T>(path: string, init?: RequestInit, timeoutMs = 30_000): Promise<T> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  let res: Response
  try {
    res = await fetch(`${BASE}${path}`, {
      headers: { 'Content-Type': 'application/json', ...init?.headers },
      signal: controller.signal,
      ...init,
    })
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw new Error(`Request timed out after ${timeoutMs / 1000}s`)
    throw e
  } finally {
    clearTimeout(timer)
  }
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }))
    const detail = body.detail
    const msg = typeof detail === 'string'
      ? detail
      : typeof detail === 'object' && detail !== null
        ? (detail.message ?? JSON.stringify(detail))
        : `HTTP ${res.status}`
    throw new Error(msg)
  }
  return res.json()
}

// Non-throwing variant for genuine raw-Response carve-outs (#1242 / Option B): callers
// that branch on the HTTP status code or read the response body REGARDLESS of ok-ness
// cannot use `request<T>` (which throws on non-2xx and returns only the parsed body).
// `requestRaw` mirrors a raw `fetch(...)` + `await r.json()` for the response-handling
// path: it resolves `{ ok, status, data }` for ANY HTTP status, and (like raw fetch /
// r.json()) PROPAGATES a network error or an unparseable body so the caller's existing
// catch handles it. The ONE intentional addition over a bare raw fetch is a 30s abort
// timeout (same default as `request<T>`) — a safety net so a hung request can't wait
// forever; it surfaces through the same caller catch. Keeps URL/version centralized
// while preserving the raw-Response control flow.
//
// `opts.tolerantParse` (#1242 slice 5) makes the body parse NON-throwing: `data`
// resolves to `null` on an empty / non-JSON body instead of throwing. This mirrors a
// raw site that guarded its parse (`await r.json().catch(() => …)`) or never parsed on
// the taken branch (status-branch / assume-success). Default false preserves the strict
// contract for every existing caller (additive, zero blast radius). `data` stays typed
// `T`; tolerant callers must null-guard (`data?.field`).
async function requestRaw<T>(
  path: string,
  init?: RequestInit,
  timeoutMs = 30_000,
  opts?: { tolerantParse?: boolean },
): Promise<{ ok: boolean; status: number; statusText: string; data: T }> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), timeoutMs)
  let res: Response
  try {
    res = await fetch(`${BASE}${path}`, {
      headers: { 'Content-Type': 'application/json', ...init?.headers },
      signal: controller.signal,
      ...init,
    })
  } catch (e) {
    if ((e as Error).name === 'AbortError') throw new Error(`Request timed out after ${timeoutMs / 1000}s`)
    throw e
  } finally {
    clearTimeout(timer)
  }
  const data = (opts?.tolerantParse ? await res.json().catch(() => null) : await res.json()) as T
  return { ok: res.ok, status: res.status, statusText: res.statusText, data }
}

// ── Types ─────────────────────────────────────────────────────────────────
// Response/DTO interfaces live in ./types (extracted #1302 linecount drain).
// Re-exported here so existing `import type { … } from '@/api/client'` callers
// keep working; the explicit `import type` brings the names this module uses
// internally into scope.
export * from './types'
import type {
  PlatformStatus, WizardStep, WizardRunResponse, AppStatus, CatalogEntry,
  HealthCheck, HealthSummary, PendingAction, AgentHealthCheck, SystemProfile,
  InfraSlot, RoutingConfig, StorageSource, GGUFModel, RecommendedModel,
  IntegrityStatus, SpineAdvisory, OllamaSetupJob, ControlPlanePosture,
} from './types'

// ── Platform ───────────────────────────────────────────────────────────────

export const platform = {
  status: () => request<PlatformStatus>('/platform/status'),
  wizardSteps: () => request<WizardStep[]>('/platform/wizard/steps'),
  wizardValidate: (data: Record<string, unknown>) =>
    request('/platform/wizard/validate', { method: 'POST', body: JSON.stringify(data) }),
  wizardRun: (data: Record<string, unknown>) =>
    request<WizardRunResponse>('/platform/wizard/run', { method: 'POST', body: JSON.stringify(data) }, 180_000),
  reset: () => request('/platform/reset?confirm=RESET_PLATFORM', { method: 'POST' }),
  // Full factory reset — backend requires ?confirm=DESTROY_ALL_DATA (platform.py /reset/full).
  resetFull: () => request('/platform/reset/full?confirm=DESTROY_ALL_DATA', { method: 'POST' }),
  // Wizard supporting reads (#1219 — SetupView migration).
  stacks: () => request<any>('/platform/stacks'),
  // Quick-Stacks CRUD (#1219 — SettingsView migration).
  createStack: (body: { label: string; app_keys: string[]; ram_note: string }) =>
    request<any>('/platform/stacks', { method: 'POST', body: JSON.stringify(body) }),
  updateStack: (stackId: string, body: { label: string; app_keys: string[]; ram_note: string }) =>
    request<any>(`/platform/stacks/${stackId}`, { method: 'PUT', body: JSON.stringify(body) }),
  timezones: () => request<any>('/platform/timezones'),
  dnsProviders: () => request<any>('/platform/dns-providers'),
  prereqs: (force = false) => request<any>(`/platform/prereqs${force ? '?force=1' : ''}`),
  certStatus: () => request<{ cert_found: boolean; message: string }>('/platform/cert-status'),
  stackAppKeys: (stackIds: string[]) =>
    request<{ keys: string[] }>(`/platform/wizard/stack-app-keys?stack_ids=${stackIds.join(',')}`),
  // Ollama setup flow (#1219 slice — setup-ollama POST + status poll + model list).
  setupOllama: (model: string) =>
    request<{ job_id: string; model: string }>('/platform/wizard/setup-ollama', { method: 'POST', body: JSON.stringify({ model }) }),
  ollamaStatus: (jobId: string) => request<OllamaSetupJob>(`/platform/wizard/ollama-status/${jobId}`),
  // Raw wizard job-status poll (#1242 — SetupView reads steps/done regardless of HTTP
  // status and tolerates non-ok to keep polling; via requestRaw).
  wizardStatusRaw: (jobId: string) =>
    requestRaw<{ steps?: any[]; done?: boolean; platform_ready?: boolean }>(`/platform/wizard/status/${jobId}`),
  // Secret validation (#1242 — SetupView net-vs-http carve-out: distinct fallback on HTTP
  // non-ok (validation result body) vs a network error (separate catch). tolerantParse so
  // an HTTP-non-ok body never throws; a network error still rejects to the caller's catch).
  validateSecretsRaw: (body: Record<string, unknown>) =>
    requestRaw<{ ok?: boolean; errors?: any[]; warnings?: string[] }>('/platform/wizard/validate-secrets', { method: 'POST', body: JSON.stringify(body) }, 30_000, { tolerantParse: true }),
  ollamaModels: (ollamaUrl: string) =>
    request<{ live: boolean; models: string[] }>(`/platform/ollama-models?ollama_url=${encodeURIComponent(ollamaUrl)}`),
  // Cloud-model probe (#1242 — SettingsView reads {error,models} body regardless of status). Via requestRaw.
  cloudModels: (provider: string, apiKey: string) =>
    requestRaw<{ error?: string; models?: string[] }>(`/platform/cloud-models?provider=${encodeURIComponent(provider)}&api_key=${encodeURIComponent(apiKey)}`),
  // Wizard POST helpers (#1219 slice B — SetupView migration).
  bcryptUsers: (body: { username: string; password: string }) =>
    request<{ users: string }>('/platform/wizard/bcrypt-users', { method: 'POST', body: JSON.stringify(body) }),
  saveLlm: (body: Record<string, unknown>) =>
    request('/platform/wizard/save-llm', { method: 'POST', body: JSON.stringify(body) }),
  // Stack delete/restore (#1242 — silent-on-non-ok carve-outs: act only when r.ok,
  // read d.action on success; via requestRaw to keep the no-else control flow).
  deleteStack: (stackId: string) =>
    requestRaw<{ action?: string }>(`/platform/stacks/${stackId}`, { method: 'DELETE' }),
  restoreStack: (stackId: string) =>
    requestRaw<unknown>(`/platform/stacks/${stackId}/restore`, { method: 'POST' }),
}

// ── Apps ───────────────────────────────────────────────────────────────────

export const apps = {
  list: () => request<AppStatus[]>('/apps'),
  get: (key: string) => request<AppStatus>(`/apps/${key}`),
  install: (key: string, opts?: Record<string, unknown>) =>
    request(`/apps/${key}/install`, { method: 'POST', body: JSON.stringify(opts ?? {}) }),
  installProgress: (key: string) => request<{done: boolean; ok: boolean | null; steps: any[]; error: string | null}>(`/apps/${key}/install/progress`),
  // Pin a container image tag (#1237 — useAppDetail assume-success migration).
  pinVersion: (key: string, imageTag: string) =>
    request(`/apps/${key}/pin-version`, { method: 'PUT', body: JSON.stringify({ image_tag: imageTag }) }),
  // Compose linter (#1242 — SettingsView assigns the scanner result body regardless of status). Via requestRaw.
  lintCompose: (yaml: string) =>
    requestRaw<any>('/apps/lint-compose', { method: 'POST', body: JSON.stringify({ yaml }) }),
  // Raw-Response app-detail mutations (#1242 — useAppDetail reads data.ok/detail/message
  // regardless of HTTP status; enhance also reads r.ok). Via requestRaw.
  update: (key: string) =>
    requestRaw<{ ok?: boolean; detail?: string }>(`/apps/${key}/update`, { method: 'POST' }),
  enhance: (key: string, body: Record<string, unknown>) =>
    requestRaw<{ message?: string }>(`/apps/${key}/enhance`, { method: 'POST', body: JSON.stringify(body) }),
  // Fire-and-forget image prefetch (#1219 slice B — SetupView migration).
  batchPrefetch: (keys: string[]) =>
    request(`/apps/batch/prefetch`, { method: 'POST', body: JSON.stringify({ keys }) }),
  // App-detail reads (#1219 — useAppDetail composable migration).
  postInstallSteps: (key: string) => request<any[]>(`/apps/${key}/post-install-steps`),
  healthConfig: (key: string) => request<any>(`/apps/${key}/health-config`),
  config: (key: string) => request<any>(`/apps/${key}/config`),
  remove: (key: string, deleteConfig = false) =>
    request(`/apps/${key}`, { method: 'DELETE', body: JSON.stringify({ delete_config: deleteConfig }) }),
  disable: (key: string, reason = 'user_request') =>
    request(`/apps/${key}/disable`, { method: 'POST', body: JSON.stringify({ reason }) }),
  enable: (key: string) =>
    request(`/apps/${key}/enable`, { method: 'POST' }),
  logs: (key: string, tail = 100) =>
    request<{ key: string; logs: string }>(`/apps/${key}/logs?tail=${tail}`),
  restart: (key: string) =>
    request(`/apps/${key}/restart`, { method: 'POST' }),
  systemProfile: () => request<SystemProfile>('/apps/system/profile'),
  // Batch + custom-install orchestration (#1242 — install-orch carve-outs that branch on
  // HTTP status and read the error/result body; via requestRaw to preserve raw-Response flow).
  batchPreflight: (keys: string[]) =>
    requestRaw<any>('/apps/batch/preflight', { method: 'POST', body: JSON.stringify({ keys }) }),
  batchInstall: (keys: string[]) =>
    requestRaw<any>('/apps/batch/install', { method: 'POST', body: JSON.stringify({ keys }) }),
  installCustom: (body: { manifest: any; compose_yaml: string }) =>
    requestRaw<{ key?: string; detail?: string }>('/apps/install-custom', { method: 'POST', body: JSON.stringify(body) }),
  // Raw install POST (reads err.detail on non-ok); distinct from the throwing `install` above.
  // `tolerantParse` (default false = strict, slice-1 callers unaffected) for the SetupView
  // install site that guarded its error-body parse with `.catch(() => ({}))`.
  installRaw: (key: string, opts?: Record<string, unknown>, tolerantParse = false) =>
    requestRaw<{ detail?: string }>(`/apps/${key}/install`, { method: 'POST', body: JSON.stringify(opts ?? {}) }, 30_000, { tolerantParse }),
  installFromGithub: (repoUrl: string) =>
    requestRaw<any>('/apps/install-from-github', { method: 'POST', body: JSON.stringify({ repo_url: repoUrl }) }),
  // Bulk install-progress map (#1219 — CatalogView failed-installs banner).
  installsProgress: () => request<{ apps: Record<string, any> }>('/apps/installs/progress'),
  // Raw bulk-progress (#1242 — SetupView probes status===404 to pick the EventSource path,
  // then silent-skips a non-ok poll WITHOUT tripping the 3-failure counter). tolerantParse so
  // a non-ok / 404 (possibly empty) body never throws — the non-ok must surface as ok:false,
  // not as a parse-throw routed to the failure counter.
  installsProgressRaw: () =>
    requestRaw<{ apps?: Record<string, any> }>('/apps/installs/progress', undefined, 30_000, { tolerantParse: true }),
  // Health-path auto-detect probe (#1219 — useAppDetail migration).
  probePath: (key: string, path: string) =>
    request<{ reachable: boolean; status: number }>(`/apps/${key}/probe-path`, { method: 'POST', body: JSON.stringify({ path }) }),
  // Save app config (#1242 — useAppDetail text-delta carve-out: distinct toast on
  // HTTP non-ok vs network catch; reads only r.ok, so via requestRaw).
  saveConfig: (key: string, values: Record<string, unknown>) =>
    requestRaw<unknown>(`/apps/${key}/config`, { method: 'PUT', body: JSON.stringify({ values }) }),
  // Raw install-progress poll (#1242 — SetupView install loops read the body regardless
  // and tolerate a non-ok to keep polling; the throwing `installProgress` would abort the
  // poll on a transient non-ok, so via requestRaw).
  installProgressRaw: (key: string) =>
    requestRaw<{ done?: boolean; ok?: boolean | null; steps?: any[]; error?: string | null }>(`/apps/${key}/install/progress`),
}

// ── Agent ──────────────────────────────────────────────────────────────────

export const agent = {
  // SLOP AI Agent diagnoses (#1219 — CatalogView migration).
  diagnoses: () => request<{ diagnoses: any[] }>('/agent/diagnoses'),
  // Apply a suggested fix (#1242 — DiagnosisCard status-branches on 501 'coming soon'
  // vs other non-ok; body is ignored, so via requestRaw).
  applyFix: (id: number | string) =>
    requestRaw<unknown>(`/agent/fixes/${id}/apply`, { method: 'POST' }),
}

// ── Chat (control plane) ─────────────────────────────────────────────────────

export const chat = {
  // Control-plane chat (#1242 — ChatView status-branches on 401/403 → 'locked' reply
  // vs the parsed body; via requestRaw).
  post: (body: Record<string, unknown>) =>
    requestRaw<any>('/chat', { method: 'POST', body: JSON.stringify(body) }),
}

// ── Observability (audit log + timeline) ─────────────────────────────────────

export const observability = {
  // Audit log + activity timeline (#1242 — both throw `${status}: ${statusText}` on
  // non-ok then read the body on ok; via requestRaw to keep the exact status-text throw).
  audit: (params: string) =>
    requestRaw<{ rows?: any[] }>(`/audit?${params}`),
  timeline: (params: string) =>
    requestRaw<{ events?: any[] }>(`/timeline?${params}`),
}

// ── Quick-start wizard ───────────────────────────────────────────────────────

export const quickstart = {
  // Quick-start phase state (#1219 — QuickStartWizard migration).
  get: () => request<any>('/quickstart'),
  // Fire-and-forget phase mark + dismiss (#1242 — assume-success carve-outs: no ok-check,
  // result ignored. tolerantParse so an empty/non-JSON body never throws where the raw
  // `fetch` (which never parsed) did not; a network error still rejects, as before).
  markPhase: (id: string, status: string) =>
    requestRaw<unknown>(`/quickstart/${id}`, { method: 'PUT', body: JSON.stringify({ status }) }, 30_000, { tolerantParse: true }),
  dismiss: () =>
    requestRaw<unknown>('/quickstart/dismiss', { method: 'POST' }, 30_000, { tolerantParse: true }),
}

// ── Update manager ───────────────────────────────────────────────────────────

export const updates = {
  // Per-container update preferences (#1219 — SettingsView migration).
  savePreferences: (preferences: Record<string, unknown>) =>
    request('/updates/preferences', { method: 'PUT', body: JSON.stringify({ preferences }) }),
  // Update-availability status (#1242 — SettingsView status-branches on 503 'Docker
  // unreachable' vs other non-ok; reads body only on ok, so via requestRaw).
  status: () => requestRaw<{ containers?: any[] }>('/updates/status'),
}

// ── Catalog ────────────────────────────────────────────────────────────────

export const catalog = {
  all: () => request<Record<string, CatalogEntry[]>>('/catalog'),
  get: (key: string) => request<CatalogEntry>(`/catalog/${key}`),
}

// ── Health ─────────────────────────────────────────────────────────────────

export const health = {
  allApps: () => request<HealthCheck[]>('/health/apps'),
  app: (key: string) => request<HealthCheck[]>(`/health/apps/${key}`),
  // Container readiness probe (#1219 slice C — SetupView install health-race check).
  containerStatus: (key: string) => request<{ ready: boolean }>(`/health/apps/${key}/container-status`),
  agentChecks: () => request<AgentHealthCheck[]>('/health/agent'),
  llmAgent: () => request<{ status: string; description: string; last_error: string; last_error_type: string; ollama_url: string; model_tried: string; consecutive_failures: number; consecutive_slow: number; last_success_at: number; configured_provider: string }>('/health/llm-agent'),
  integrity: () => request<IntegrityStatus>('/health/integrity'),
  advisories: (limit = 100) => request<{ advisories: SpineAdvisory[] }>(`/health/advisories?limit=${limit}`),
  summary: () => request<HealthSummary>('/health/summary'),
  pendingActions: () => request<PendingAction[]>('/health/pending-actions'),
  // Pending-fixes / escalation (#1219 — migrate HealthView raw fetch → typed client).
  pendingFixes: () => request<any[]>('/health/pending-fixes'),
  approvePendingFix: (id: string) => request<any>(`/health/pending-fixes/${id}/approve`, { method: 'POST' }),
  rejectPendingFix: (id: string) => request<any>(`/health/pending-fixes/${id}/reject`, { method: 'POST' }),
  escalateFix: (body: { app_key: string; check_name: string; problem: string; logs?: string; context?: string }) =>
    request<any>('/health/escalate', { method: 'POST', body: JSON.stringify(body) }),
  applyFix: (body: { app_key: string; action_type: string; suggested_fix: string; problem: string }) =>
    request<any>('/health/apply-fix', { method: 'POST', body: JSON.stringify(body) }),
  runCycle: () => request<any>('/health/run', { method: 'POST' }),
  // Raw-Response health endpoints (#1242 — SettingsView reads the body regardless of
  // HTTP status; ghostAction also branches on r.ok). Via requestRaw.
  llmTest: (body: { provider: string; api_key: string; model: string }) =>
    requestRaw<{ ok: boolean; error?: string; latency_ms?: number }>('/health/llm-test', { method: 'POST', body: JSON.stringify(body) }),
  ghostAction: (body: { resource_type: string; name: string; action: string }) =>
    requestRaw<{ message: string; detail?: string }>('/health/ghost-resources/action', { method: 'POST', body: JSON.stringify(body) }),
  agentConfig: () => request<any>('/health/agent-config'),
  setAgentConfig: (qs: string) => request<any>(`/health/agent-config?${qs}`, { method: 'PUT' }),
  scheduler: () => request<any>('/health/scheduler'),
  pauseScheduler: () => request('/health/scheduler/pause', { method: 'POST' }),
  // Sources / maintenance-windows / anomalies / weekly-summary (#1219 slice 2 — migrate HealthView raw fetch).
  sources: () => request<any>('/health/sources'),
  scanSources: () => request<any>('/health/sources/scan', { method: 'POST' }),
  findSourceReplacement: (body: { source_type: string; resource_key: string; url: string }) =>
    request<any>('/health/sources/find-replacement', { method: 'POST', body: JSON.stringify(body) }),
  applySourceReplacement: (body: { source_type: string; resource_key: string; old_url: string; new_url: string }) =>
    request<any>('/health/sources/apply-replacement', { method: 'POST', body: JSON.stringify(body) }),
  maintenanceWindows: () => request<any>('/health/maintenance-windows'),
  createMaintenanceWindow: (body: Record<string, unknown>) =>
    request<any>('/health/maintenance-windows', { method: 'POST', body: JSON.stringify(body) }),
  deleteMaintenanceWindow: (id: number) => request<any>(`/health/maintenance-windows/${id}`, { method: 'DELETE' }),
  anomalies: () => request<any>('/health/anomalies'),
  snoozeAnomaly: (appKey: string, checkName: string, body: { app_key: string; check_name: string; hours: number }) =>
    request<any>(`/health/anomalies/${appKey}/${checkName}/snooze`, { method: 'POST', body: JSON.stringify(body) }),
  weeklySummary: () => request<any>('/health/weekly-summary'),
  llmPing: () => request<any>('/health/llm-ping', undefined, 8000),
  llmProviders: () => request<any>('/health/llm-providers'),
  ghostResources: () => request<any>('/health/ghost-resources'),
  updateSettings: (params: { interval_secs?: number; ntfy_topic?: string; ollama_url?: string }) => {
    const qs = Object.entries(params)
      .filter(([, v]) => v !== undefined)
      .map(([k, v]) => `${k}=${encodeURIComponent(String(v))}`)
      .join('&')
    return request(`/health/settings?${qs}`, { method: 'PUT' })
  },
}

// ── Infra ───────────────────────────────────────────────────────────────────

export const infra = {
  slots: () => request<InfraSlot[]>('/infra/slots'),
  slot: (slot: string) => request<InfraSlot>(`/infra/slots/${slot}`),
  providers: (slot?: string) =>
    request(`/infra/providers${slot ? `/${slot}` : ''}`),
  providerSchema: (slot: string) => request(`/infra/providers/${slot}/schema`),
  deploy: (slot: string, provider: string, cfg: Record<string, unknown>) =>
    request(`/infra/${slot}/deploy`, { method: 'POST', body: JSON.stringify({ provider, config: cfg }) }),
  swap: (slot: string, toProvider: string, cfg: Record<string, unknown>) =>
    request(`/infra/${slot}/swap`, { method: 'POST', body: JSON.stringify({ to_provider: toProvider, config: cfg }) }),
  // Raw-Response verify endpoints (#1242 — InfraView reads the {ok,message} body
  // regardless of HTTP status; slotVerify also reads r.ok/r.status). Via requestRaw.
  tunnelVerify: (providerKey: string) =>
    requestRaw<{ ok: boolean; message: string }>(`/infra/tunnel/verify?provider_key=${providerKey}`, { method: 'POST' }),
  tunnelRemove: (providerKey: string) =>
    requestRaw<{ ok: boolean; message: string }>(`/infra/tunnel/remove?provider_key=${providerKey}`, { method: 'POST' }),
  slotVerify: (slot: string) =>
    requestRaw<{ ok?: boolean; message?: string }>(`/infra/${slot}/verify`, { method: 'POST' }),
}

// ── Routing ─────────────────────────────────────────────────────────────────

export const routing = {
  media: () => request<RoutingConfig[]>('/routing/media'),
  updateMedia: (type: string, data: Record<string, unknown>) =>
    request<RoutingConfig>(`/routing/media/${type}`, { method: 'PUT', body: JSON.stringify(data) }),
  seerrHelp: (type: string) => request(`/routing/media/${type}/seerr-help`),
  instances: () => request('/routing/instances'),
  installInstance: (manifestKey: string, data: Record<string, unknown>) =>
    request(`/routing/instances/${manifestKey}`, { method: 'POST', body: JSON.stringify(data) }),
  removeInstance: (instanceKey: string) =>
    request(`/routing/instances/${instanceKey}`, { method: 'DELETE' }),
}

// ── Storage ─────────────────────────────────────────────────────────────────

export const storage = {
  list: () => request<StorageSource[]>('/storage/sources'),
  add: (data: Record<string, unknown>) =>
    request<StorageSource>('/storage/sources', { method: 'POST', body: JSON.stringify(data) }),
  config: (id: number) => request(`/storage/sources/${id}/config`),
  verify: (id: number) => request(`/storage/sources/${id}/verify`, { method: 'POST' }),
  remove: (id: number) => request(`/storage/sources/${id}`, { method: 'DELETE' }),
}

// ── Control-plane auth posture (#1250 / #976 Phase-C) ───────────────────────

export const controlPlane = {
  posture: () => request<ControlPlanePosture>('/control-plane/posture'),
}

// ── Settings ──────────────────────────────────────────────────────────────

export const settings = {
  get: () => request<Record<string, unknown>>('/settings'),
  update: (data: Record<string, unknown>) =>
    request<Record<string, unknown>>('/settings', { method: 'PUT', body: JSON.stringify(data) }),
  system: () => request<Record<string, unknown>>('/settings/system'),
  secrets: () => request<any>('/settings/secrets'),
  // Settings sub-resources (#1219 — SettingsView read migration).
  cloudLlm: () => request<any>('/settings/cloud-llm'),
  aiSafety: () => request<{ levels: any }>('/settings/ai-safety'),
  preapproval: () => request<any>('/settings/preapproval'),
  traefik: () => request<any>('/settings/traefik'),
  // Settings PUT helpers (#1219 — SettingsView mutating-site migration).
  updateSecrets: (updates: Record<string, string>) =>
    request('/settings/secrets', { method: 'PUT', body: JSON.stringify({ updates }) }),
  // Raw variant (#1242 — saveHFToken text-delta: distinct 'Could not save token.' on
  // HTTP non-ok vs network catch 'Save failed.'; reads only r.ok, so via requestRaw).
  updateSecretsRaw: (updates: Record<string, string>) =>
    requestRaw<unknown>('/settings/secrets', { method: 'PUT', body: JSON.stringify({ updates }) }),
  updateCloudLlm: (body: { monthly_limit_usd?: number; provider?: string; model?: string; active_providers?: string[]; api_keys?: Record<string, string>; cascade?: string[] }) =>
    request('/settings/cloud-llm', { method: 'PUT', body: JSON.stringify(body) }),
  updateAiSafety: (body: { action_type: string; level: string }) =>
    request('/settings/ai-safety', { method: 'PUT', body: JSON.stringify(body) }),
  // Raw-Response: SettingsView reads {ok,message,detail} body regardless of status (#1242). Via requestRaw.
  // Note: the backend returns only {ok:true} on success (no message) and {detail} on a
  // 422 — so `message` is optional; the caller supplies a fallback success string.
  updateTraefik: (body: Record<string, unknown>) =>
    requestRaw<{ ok: boolean; message?: string; detail?: string }>('/settings/traefik', { method: 'PUT', body: JSON.stringify(body) }),
  updateTier: (body: { tier: number; pre_approved: boolean }) =>
    request<any>('/settings/preapproval/tier', { method: 'PUT', body: JSON.stringify(body) }),
  // Per-app override (tier × scope axis — #1070). PUT sets, DELETE clears one tier
  // (?tier=N) or the whole app. Both return the fresh effective policy view.
  updateApp: (body: { app_key: string; tier: number; pre_approved: boolean }) =>
    request<any>('/settings/preapproval/app', { method: 'PUT', body: JSON.stringify(body) }),
  clearApp: (appKey: string, tier?: number) =>
    request<any>(
      `/settings/preapproval/app/${encodeURIComponent(appKey)}${tier != null ? `?tier=${tier}` : ''}`,
      { method: 'DELETE' },
    ),
}

// ── Models (LLM/GGUF) ────────────────────────────────────────────────────

export const models = {
  list: () => request<GGUFModel[]>('/models/gguf'),
  recommended: () => request<RecommendedModel[]>('/models/recommended'),
  validate: (path: string) =>
    request<any>('/models/gguf/validate', { method: 'POST', body: JSON.stringify({ path }) }),
  ggufPreflight: (url: string) =>
    request<any>(`/models/gguf/preflight?url=${encodeURIComponent(url)}`, { method: 'POST' }),
  hfSearch: (q: string) => request<any>(`/models/hf/search?q=${encodeURIComponent(q)}`),
  downloadSSE: (url: string, filename?: string): EventSource => {
    // GET endpoint required for EventSource (browser SSE API only supports GET)
    // Backend emits: progress, complete, error events
    const qs = `url=${encodeURIComponent(url)}${filename ? `&filename=${encodeURIComponent(filename)}` : ''}`
    return new EventSource(`${BASE}/models/gguf/download?${qs}`)
  },
  remove: (filename: string) => request(`/models/gguf/${filename}`, { method: 'DELETE' }),
  agentConfig: () => request('/models/agent/config'),
  setAgentConfig: (cfg: Record<string, unknown>) =>
    request('/models/agent/config', { method: 'POST', body: JSON.stringify(cfg) }),
  evaluate: () => request('/models/agent/evaluate', { method: 'POST' }),
  // Thumbs feedback → outcome-weighted learning store (#1219 — HealthView migration).
  recordFixOutcome: (body: { app_key: string; error_type: string; context: string; suggested_fix: string; outcome: string }) =>
    request('/models/fix-history', { method: 'POST', body: JSON.stringify(body) }),
  // Routing registry (#1219 — ModelsView migration).
  routingLog: (limit = 40) => request<any>(`/models/routing-log?limit=${limit}`, undefined, 10_000),
  registry: () => request<any>('/models/registry'),
  updateRegistryModel: (filename: string, body: Record<string, unknown>) =>
    request<any>(`/models/registry/${encodeURIComponent(filename)}`, { method: 'PUT', body: JSON.stringify(body) }),
  evaluateHardware: (modelSizeGb: number) =>
    request<any>(`/models/evaluate-hardware?model_size_gb=${modelSizeGb}`),
}
