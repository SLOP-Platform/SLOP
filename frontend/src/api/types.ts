// src/api/types.ts
// Shared response/DTO types for the SLOP backend API.
//
// Extracted from client.ts (#1302 linecount drain) so the typed client module
// stays under its size cap. These are re-exported from `./client`, so existing
// `import type { … } from '@/api/client'` call sites keep working unchanged.

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
  // subject_type='agent' rows (GET /health/agent): agent_status writes
  // running/disabled/error/unknown; supervisor task rows write ok/error.
  status: 'ok' | 'running' | 'error' | 'disabled' | 'unknown'
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

// Store-only spine advisory row — advisory LLM annotation persisted alongside a
// GROUND verdict for human review (read surface added in #1089, GET /health/advisories).
// `annotation` is the parsed JSON object, or raw text if it failed to parse, or null.
export interface SpineAdvisory {
  id: number
  finding_id: string
  verdict: string
  annotation: Record<string, unknown> | string | null
  provider: string
  created_at: number
}

export interface OllamaSetupJob {
  id?: string
  model: string
  phase: 'starting' | 'installing' | 'pulling' | 'done' | 'error'
  progress: number
  message: string
  done: boolean
  ok: boolean
  errorDetail?: string | null
}

// Control-plane auth posture (#1250 / #976 Phase-C)
export interface ControlPlanePosture {
  mode: string // off | observe | enforce
  token_provisioned: boolean
  posture: string // red | amber | green
  observe_would_reject_count: number
}
