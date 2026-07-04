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

// ── Types ─────────────────────────────────────────────────────────────────

export interface PlatformStatus {
  status: 'pending' | 'ready' | 'error'
  domain: string | null
  network_name: string
  config_root: string
  media_root: string
  puid: number
  pgid: number
  timezone: string
  traefik_version: string | null
  installed_at: number | null
}

export interface WizardStep {
  name: string
  title: string
  description: string
}

export interface WizardStepResult {
  step: string
  status: 'ok' | 'error' | 'skipped'
  message: string
  detail: string
}

export interface WizardRunResponse {
  ok: boolean
  platform_ready: boolean
  steps: WizardStepResult[]
  error: string | null
}

export interface AppStatus {
  key: string
  display_name: string
  status: string
  category: string
  tier: number
  image: string
  host_port: number | null
  web_port: number | null
  config_path: string
  criticality: string
  installed_at: number | null
  manifest_hash: string | null
}

export interface CatalogEntry {
  key: string
  display_name: string
  description: string
  category: string
  tier: number
  icon: string
  web_port: number | null
  linuxserver: boolean
  tags: string[]
  links: Record<string, string>
  has_gpu: boolean
  gpu_optional: boolean | null
  hardware_note: string | null
  start_grace_s: number
  dependencies: { postgres: boolean; redis: boolean; mariadb: boolean; apps: string[] }
}

export interface HealthCheck {
  app_key: string
  check_name: string
  status: 'ok' | 'warning' | 'error' | 'unknown'
  summary: string
  last_checked: string | null
  auto_fix: string | null
  last_checked_age_seconds: number | null
}

export interface HealthSummary {
  ok: number
  warning: number
  error: number
  unknown: number
  agent_status: string
  process_integrity_status: string
  last_cycle_age_seconds: number | null
  scheduler_alive: boolean
}

export interface PendingAction {
  priority: 'error' | 'warning' | 'suggestion'
  title: string
  description: string
  action: string
  link: string | null
  icon: string
}

export interface AgentHealthCheck {
  check_name: string
  status: 'running' | 'error' | 'disabled' | 'unknown'
  summary: string
  detail: string | null
  last_checked: string | null
}

export interface SystemProfile {
  cpu_cores: number
  cpu_model: string
  total_ram_gb: number
  free_ram_gb: number
  headroom_ram_gb: number
  docker_ram_gb: number
  architecture: string
  disks: { path: string; total_gb: number; free_gb: number; percent_used: number }[]
  estimated_stack_ram_gb: number
  recommended_llm_model: string
  available_llm_models: string[]
  llm_warning: string | null
  measured_at: number
  note: string
}

export interface InfraSlot {
  slot: string
  provider: string | null
  status: string
  display_name: string | null
  deployed_at: number | null
}

export interface RoutingConfig {
  media_type: string
  canonical_manifest: string
  debrid_instance: string | null
  download_instance: string | null
  default_path: string
  seerr_supported: boolean
  notes: string | null
}

export interface StorageSource {
  id: number | null
  name: string
  source_type: string
  remote_host: string | null
  remote_path: string | null
  mount_point: string
  is_primary: boolean
  status: string
  error_message: string | null
  options: Record<string, unknown>
}

export interface GGUFModel {
  filename: string
  path: string
  size_mb: number
  valid: boolean
  gguf_version: number | null
  error: string | null
  warning: string | null
}

export interface RecommendedModel {
  name: string
  hf_url: string
  size_gb: number
  recommended_for: string
  notes: string
}

export interface IntegrityStatus {
  status: string
  critical_gaps: number
  high_gaps: number
  total_rules: number
  summary: string
  checked_at: number
}

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
}

// ── Apps ───────────────────────────────────────────────────────────────────

export const apps = {
  list: () => request<AppStatus[]>('/apps'),
  get: (key: string) => request<AppStatus>(`/apps/${key}`),
  install: (key: string, opts?: Record<string, unknown>) =>
    request(`/apps/${key}/install`, { method: 'POST', body: JSON.stringify(opts ?? {}) }),
  installProgress: (key: string) => request<{done: boolean; ok: boolean | null; steps: any[]; error: string | null}>(`/apps/${key}/install/progress`),
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
  agentChecks: () => request<AgentHealthCheck[]>('/health/agent'),
  llmAgent: () => request<{ status: string; description: string; last_error: string; last_error_type: string; ollama_url: string; model_tried: string; consecutive_failures: number; consecutive_slow: number; last_success_at: number; configured_provider: string }>('/health/llm-agent'),
  integrity: () => request<IntegrityStatus>('/health/integrity'),
  summary: () => request<HealthSummary>('/health/summary'),
  pendingActions: () => request<PendingAction[]>('/health/pending-actions'),
  runCycle: () => request('/health/run', { method: 'POST' }),
  scheduler: () => request('/health/scheduler'),
  pauseScheduler: () => request('/health/scheduler/pause', { method: 'POST' }),
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

// ── Settings ──────────────────────────────────────────────────────────────

export const settings = {
  get: () => request<Record<string, unknown>>('/settings'),
  update: (data: Record<string, unknown>) =>
    request<Record<string, unknown>>('/settings', { method: 'PUT', body: JSON.stringify(data) }),
  system: () => request<Record<string, unknown>>('/settings/system'),
}

// ── Models (LLM/GGUF) ────────────────────────────────────────────────────

export const models = {
  list: () => request<GGUFModel[]>('/models/gguf'),
  recommended: () => request<RecommendedModel[]>('/models/recommended'),
  validate: (path: string) =>
    request('/models/gguf/validate', { method: 'POST', body: JSON.stringify({ path }) }),
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
}
