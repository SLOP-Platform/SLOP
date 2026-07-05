-- Migration 001: baseline
--
-- This migration is the byte-equivalent (modulo this comment header) of
-- backend/core/schema.sql at the moment v4 migrations were introduced.
--
-- It runs only on FRESH databases. Existing v3 DBs are auto-stamped past
-- this migration by the runner (see backend/core/migrations.py and the
-- design doc at docs/cleanup/01_migrations_design.md).
--
-- This file is IMMUTABLE. Schema changes go in 002+. The runner enforces
-- this with a SHA256 checksum recorded in schema_migrations.
-- ===========================================================================

-- SLOP v3 — SQLite State Schema
-- This database is the single source of truth for everything SLOP
-- has deployed, configured, and provisioned. Nothing is written to disk
-- (compose files, .env, CF hostnames) without a corresponding state record.
--
-- All tables use INTEGER PRIMARY KEY (SQLite rowid alias) for speed.
-- Timestamps are Unix epoch integers — no timezone ambiguity.
-- JSON columns store structured data that doesn't need querying.

PRAGMA journal_mode = WAL;      -- concurrent reads during writes
PRAGMA foreign_keys = ON;       -- enforce referential integrity
PRAGMA synchronous = NORMAL;    -- safe + fast (WAL mode)


-- ---------------------------------------------------------------------------
-- Platform
-- Tracks the one-time platform setup (Traefik + Docker network).
-- Only one row ever exists. Status drives the setup wizard gate.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS platform (
    id              INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'ready', 'error')),
    domain          TEXT,           -- e.g. example.com
    wildcard_domain TEXT,           -- e.g. *.example.com
    network_name    TEXT NOT NULL DEFAULT 'slop',
    config_root     TEXT NOT NULL DEFAULT '/srv/slop/config',
    media_root      TEXT NOT NULL DEFAULT '/mnt/media',
    puid            INTEGER NOT NULL DEFAULT 1000,
    pgid            INTEGER NOT NULL DEFAULT 1000,
    timezone        TEXT NOT NULL DEFAULT 'America/Los_Angeles',
    traefik_version TEXT,           -- deployed Traefik image tag
    cert_resolver   TEXT NOT NULL DEFAULT 'letsencrypt',
    installed_at    INTEGER,        -- Unix timestamp, NULL until ready
    updated_at      INTEGER NOT NULL DEFAULT (unixepoch())
);


-- ---------------------------------------------------------------------------
-- Infrastructure slots
-- One row per slot type. Only one provider active per slot at a time.
-- Slot types: auth | tunnel | vpn | management | dashboard
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS infra_slots (
    id              INTEGER PRIMARY KEY,
    slot            TEXT NOT NULL UNIQUE
                        CHECK (slot IN ('auth', 'tunnel', 'vpn', 'management', 'dashboard')),
    provider        TEXT,           -- e.g. tinyauth, cloudflared, gluetun
    status          TEXT NOT NULL DEFAULT 'empty'
                        CHECK (status IN ('empty', 'deploying', 'active', 'migrating', 'error', 'removed')),
    config          TEXT,           -- JSON: provider-specific config (no secrets)
    deployed_at     INTEGER,
    updated_at      INTEGER NOT NULL DEFAULT (unixepoch())
);

-- Seed the five slots on first run
INSERT OR IGNORE INTO infra_slots (slot) VALUES
    ('auth'), ('tunnel'), ('vpn'), ('management'), ('dashboard');


-- ---------------------------------------------------------------------------
-- Apps
-- Every Layer 2 app installed through SLOP.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS apps (
    id              INTEGER PRIMARY KEY,
    key             TEXT NOT NULL UNIQUE,   -- catalog key e.g. sonarr
    display_name    TEXT NOT NULL,
    tier            INTEGER NOT NULL DEFAULT 2,
    category        TEXT NOT NULL,          -- arr | media | downloader | tools | ai | ...
    status          TEXT NOT NULL DEFAULT 'installing'
                        CHECK (status IN (
                            'installing', 'running', 'stopped',
                            'unhealthy', 'updating', 'removing', 'error', 'disabled',
                            'failed'
                        )),
    image           TEXT NOT NULL,          -- Docker image used at install time
    image_tag       TEXT NOT NULL DEFAULT 'latest',
    container_name  TEXT NOT NULL,
    web_port        INTEGER,                -- internal container port
    host_port       INTEGER,               -- actual host port (may differ if conflict resolved)
    config_path     TEXT,                  -- absolute path to config folder on host
    manifest_source TEXT,                  -- 'catalog' | github URL | 'registry:key'
    manifest_hash   TEXT,                  -- SHA256 of manifest at install time
    extra_config    TEXT,                  -- JSON: user-supplied env overrides
    installed_at    INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at      INTEGER NOT NULL DEFAULT (unixepoch()),
    last_healthy_at INTEGER
);


-- ---------------------------------------------------------------------------
-- App dependencies
-- Tracks which apps depend on which managed services (PostgreSQL, Redis).
-- When the last dependent app is removed, the managed service is offered
-- for removal too.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app_dependencies (
    id              INTEGER PRIMARY KEY,
    app_id          INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    dependency_type TEXT NOT NULL CHECK (dependency_type IN ('postgres', 'redis', 'mariadb', 'app')),
    dependency_key  TEXT NOT NULL,  -- 'postgres', 'redis', or app.key
    db_name         TEXT,           -- for postgres: the database name created for this app
    db_user         TEXT,           -- for postgres: the user created for this app
    UNIQUE (app_id, dependency_type, dependency_key)
);


-- ---------------------------------------------------------------------------
-- Managed services
-- PostgreSQL and Valkey/Redis instances that auto-deploy as dependencies.
-- Unlike apps, these are not user-facing catalog entries — they're internal.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS managed_services (
    id              INTEGER PRIMARY KEY,
    service_type    TEXT NOT NULL UNIQUE CHECK (service_type IN ('postgres', 'redis', 'mariadb')),
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'running', 'error')),
    container_name  TEXT NOT NULL,
    image           TEXT NOT NULL,
    host_port       INTEGER,
    data_path       TEXT,           -- host bind-mount path
    deployed_at     INTEGER,
    updated_at      INTEGER NOT NULL DEFAULT (unixepoch())
);


-- ---------------------------------------------------------------------------
-- Wiring
-- Records every automated connection made between apps.
-- e.g. Prowlarr registered as indexer in Sonarr via API.
-- Used to re-run wiring if an app is redeployed or an API key changes.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS wiring (
    id              INTEGER PRIMARY KEY,
    source_app_id   INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    target_app_id   INTEGER NOT NULL REFERENCES apps(id) ON DELETE CASCADE,
    wire_type       TEXT NOT NULL,      -- e.g. indexer | notification | library
    status          TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'active', 'failed', 'stale')),
    config          TEXT,               -- JSON: wire-specific data
    wired_at        INTEGER,
    checked_at      INTEGER,
    UNIQUE (source_app_id, target_app_id, wire_type)
);


-- ---------------------------------------------------------------------------
-- External resources
-- Everything SLOP has provisioned outside Docker:
-- Cloudflare Tunnel hostnames, DNS records, etc.
-- On app removal, these are cleaned up via the CF API.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS external_resources (
    id              INTEGER PRIMARY KEY,
    app_id          INTEGER REFERENCES apps(id) ON DELETE SET NULL,
    resource_type   TEXT NOT NULL CHECK (resource_type IN (
                        'cf_tunnel_hostname', 'cf_dns_record', 'traefik_route'
                    )),
    resource_id     TEXT,               -- CF hostname ID or DNS record ID
    hostname        TEXT,               -- e.g. sonarr.example.com
    target          TEXT,               -- e.g. HTTPS:<HOST>:443
    config          TEXT,               -- JSON: full resource config snapshot
    provisioned_at  INTEGER NOT NULL DEFAULT (unixepoch()),
    removed_at      INTEGER             -- NULL = still active
);


-- ---------------------------------------------------------------------------
-- Secrets registry
-- Tracks which secrets are set in .env — never stores values, only keys
-- and metadata. Values live exclusively in .env on the server.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS secrets (
    id              INTEGER PRIMARY KEY,
    key             TEXT NOT NULL UNIQUE,   -- env var name e.g. CF_DNS_API_TOKEN
    description     TEXT,
    service         TEXT,                   -- which app/infra slot uses this
    is_set          INTEGER NOT NULL DEFAULT 0 CHECK (is_set IN (0, 1)),
    updated_at      INTEGER NOT NULL DEFAULT (unixepoch())
);


-- ---------------------------------------------------------------------------
-- Operations log
-- Append-only record of every significant action SLOP takes.
-- Used for the daily progress report, audit trail, and rollback reference.
-- Never deleted — rotated to a separate archive table after 90 days.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS operations (
    id              INTEGER PRIMARY KEY,
    operation       TEXT NOT NULL,          -- install | remove | update | wire | swap | heal
    subject_type    TEXT NOT NULL,          -- app | infra | platform | managed_service
    subject_key     TEXT NOT NULL,          -- app.key or slot name
    status          TEXT NOT NULL CHECK (status IN ('started', 'completed', 'failed', 'rolled_back')),
    triggered_by    TEXT NOT NULL DEFAULT 'user'
                        CHECK (triggered_by IN ('user', 'cli', 'health', 'scheduler')),
    detail          TEXT,                   -- JSON: operation-specific context
    error           TEXT,                   -- plain-language error if failed
    started_at      INTEGER NOT NULL DEFAULT (unixepoch()),
    completed_at    INTEGER
);

CREATE INDEX IF NOT EXISTS idx_operations_subject ON operations(subject_type, subject_key);
CREATE INDEX IF NOT EXISTS idx_operations_started  ON operations(started_at DESC);


-- ---------------------------------------------------------------------------
-- Health checks
-- Most recent result for each named check per app/slot.
-- One row per (subject, check_name) — upserted on every run.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS health_checks (
    id              INTEGER PRIMARY KEY,
    subject_type    TEXT NOT NULL,          -- app | infra | platform
    subject_key     TEXT NOT NULL,
    check_name      TEXT NOT NULL,          -- e.g. api_reachable | disk_space | wiring
    status          TEXT NOT NULL CHECK (status IN ('ok', 'warning', 'error', 'unknown')),
    summary         TEXT NOT NULL,          -- one-line plain-language result
    detail          TEXT,                   -- extended info for expansion
    auto_fix        TEXT,                   -- name of available auto-fix, NULL if none
    checked_at      INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE (subject_type, subject_key, check_name)
);


-- ---------------------------------------------------------------------------
-- Infrastructure migrations
-- Tracks in-progress and historical infra slot swaps.
-- If a migration is interrupted, this table drives the rollback.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS infra_migrations (
    id              INTEGER PRIMARY KEY,
    slot            TEXT NOT NULL,
    from_provider   TEXT NOT NULL,
    to_provider     TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'started'
                        CHECK (status IN ('started', 'completed', 'failed', 'rolled_back')),
    steps_total     INTEGER NOT NULL DEFAULT 0,
    steps_completed INTEGER NOT NULL DEFAULT 0,
    current_step    TEXT,                   -- description of step in progress
    rollback_data   TEXT,                   -- JSON: data needed to reverse the migration
    started_at      INTEGER NOT NULL DEFAULT (unixepoch()),
    completed_at    INTEGER
);


-- ---------------------------------------------------------------------------
-- Settings
-- Key-value store for user preferences and feature flags.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL,
    updated_at      INTEGER NOT NULL DEFAULT (unixepoch())
);

-- Defaults
INSERT OR IGNORE INTO settings (key, value) VALUES
    ('cf_auto_register_hostnames', 'true'),
    ('health_check_interval_secs', '30'),
    ('disk_warn_percent',          '80'),
    ('disk_error_percent',         '90'),
    ('progress_report_enabled',    'true'),
    ('ollama_gpu_enabled',         'auto');    -- auto | true | false


-- ---------------------------------------------------------------------------
-- Storage sources
-- External NAS, cloud storage, and remote filesystem connections.
-- LAN NAS (NFS/SMB) → systemd mount units generated on host
-- Cloud/remote      → rclone container instance deployed per source
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS storage_sources (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,       -- human label: "Main NAS", "Backblaze B2"
    source_type     TEXT NOT NULL
                        CHECK (source_type IN ('nfs', 'smb', 'rclone', 'local')),
    remote_host     TEXT,                       -- IP/hostname for nfs/smb
    remote_path     TEXT,                       -- export path or share name
    mount_point     TEXT NOT NULL UNIQUE,       -- local path: /mnt/nas01
    credentials_key TEXT,                       -- key in secrets table (smb/sftp)
    options         TEXT,                       -- JSON: mount options / rclone backend config
    is_primary      INTEGER NOT NULL DEFAULT 0, -- 1 = used as media_root fallback
    status          TEXT NOT NULL DEFAULT 'inactive'
                        CHECK (status IN ('active', 'inactive', 'error', 'mounting')),
    error_message   TEXT,
    created_at      INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at      INTEGER NOT NULL DEFAULT (unixepoch())
);

-- ---------------------------------------------------------------------------
-- App instances
-- Tracks multiple instances of the same app (e.g. sonarr_debrid + sonarr).
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app_instances (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    instance_key    TEXT NOT NULL UNIQUE,       -- e.g. sonarr_debrid
    manifest_key    TEXT NOT NULL,              -- e.g. sonarr (base manifest)
    label           TEXT NOT NULL,              -- e.g. "Sonarr (Debrid)"
    role            TEXT NOT NULL DEFAULT 'default'
                        CHECK (role IN ('default', 'debrid', 'download', 'secondary')),
    app_id          INTEGER REFERENCES apps(id) ON DELETE CASCADE,
    created_at      INTEGER NOT NULL DEFAULT (unixepoch())
);

-- Insert storage settings defaults
INSERT OR IGNORE INTO settings (key, value) VALUES
    ('storage_mount_timeout_secs',  '30'),
    ('storage_retry_on_disconnect', 'true'),
    ('nfs_version',                 '4.1'),
    ('smb_version',                 '3.0'),
    ('rclone_vfs_cache_mode',       'full'),
    ('rclone_dir_cache_time',       '10s'),
    ('rclone_buffer_size',          '64M');


-- ---------------------------------------------------------------------------
-- Request routing
-- Maps media types to arr instances for debrid/download routing.
-- Seerr (Overseerr/Jellyseerr) uses instance IDs to route requests
-- to specific Sonarr/Radarr instances per user group.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS request_routing (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    media_type      TEXT NOT NULL UNIQUE
                        CHECK (media_type IN (
                            'movies', 'tv', 'music', 'books',
                            'comics', 'audiobooks', 'adult'
                        )),
    debrid_instance  TEXT,           -- instance_key of the debrid arr (e.g. radarr_debrid)
    download_instance TEXT,          -- instance_key of the download arr (e.g. radarr)
    seerr_debrid_id  INTEGER,        -- Seerr internal ID for debrid instance
    seerr_download_id INTEGER,       -- Seerr internal ID for download instance
    default_path    TEXT NOT NULL DEFAULT 'download'
                        CHECK (default_path IN ('debrid', 'download', 'ask')),
    notes           TEXT,
    updated_at      INTEGER NOT NULL DEFAULT (unixepoch())
);

-- Seed default routing rows (all pointing to download path by default)
INSERT OR IGNORE INTO request_routing (media_type, default_path) VALUES
    ('movies',     'download'),
    ('tv',         'download'),
    ('music',      'download'),
    ('books',      'download'),
    ('comics',     'download'),
    ('audiobooks', 'download'),
    ('adult',      'download');


-- ---------------------------------------------------------------------------
-- Manifest registry
-- Tracks community manifests available from the registry URL.
-- Users can pull any entry into their local catalog with one click.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS manifest_registry (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key             TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    description     TEXT,
    category        TEXT,
    icon            TEXT DEFAULT '📦',
    source_url      TEXT NOT NULL,       -- raw URL to the YAML manifest file
    author          TEXT DEFAULT 'community',
    verified        INTEGER DEFAULT 0,   -- 1 = reviewed by maintainers
    installed       INTEGER DEFAULT 0,   -- 1 = pulled into local custom catalog
    registry_url    TEXT,               -- which registry this came from
    fetched_at      INTEGER,
    updated_at      INTEGER NOT NULL DEFAULT (unixepoch())
);

INSERT OR IGNORE INTO settings (key, value) VALUES
    ('registry_url', 'https://raw.githubusercontent.com/SLOP-Platform/SLOP/main/catalog/registry.json'),
    ('registry_last_sync', '0'),
    ('registry_auto_sync', 'true'),
    ('registry_sync_interval_hours', '24');


-- ---------------------------------------------------------------------------
-- Operation steps
-- Persists individual steps during long-running operations so the UI
-- can poll for real-time progress without SSE or WebSockets.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS operation_steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    op_key      TEXT NOT NULL,              -- app key being operated on
    step_name   TEXT NOT NULL,
    status      TEXT NOT NULL CHECK (status IN ('ok','warning','error','running','skipped')),
    message     TEXT NOT NULL DEFAULT '',
    detail      TEXT DEFAULT '',
    created_at  INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_op_steps_key ON operation_steps(op_key, created_at);

-- LLM fix history — stores error→fix→outcome triples for the feedback loop
CREATE TABLE IF NOT EXISTS fix_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    app_key     TEXT NOT NULL,
    error_type  TEXT NOT NULL,
    context     TEXT NOT NULL DEFAULT '',
    suggested_fix TEXT NOT NULL,
    outcome     TEXT NOT NULL DEFAULT 'pending',  -- pending | success | failure
    thumbs      INTEGER DEFAULT NULL,             -- 1=up, -1=down, NULL=no feedback
    created_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fix_history_app ON fix_history (app_key);
CREATE INDEX IF NOT EXISTS idx_fix_history_error ON fix_history (error_type, outcome);

-- Cloud LLM usage tracking — cost monitor and audit log for escalation calls
CREATE TABLE IF NOT EXISTS cloud_llm_usage (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    prompt_tokens   INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens    INTEGER NOT NULL DEFAULT 0,
    cost_usd        REAL NOT NULL DEFAULT 0.0,
    sanitized       INTEGER NOT NULL DEFAULT 1,  -- 1=sanitized before send
    purpose         TEXT NOT NULL DEFAULT '',     -- e.g. 'health_escalation'
    app_key         TEXT NOT NULL DEFAULT '',
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cloud_usage_provider ON cloud_llm_usage (provider);
CREATE INDEX IF NOT EXISTS idx_cloud_usage_created ON cloud_llm_usage (created_at);

-- Health check history — full time-series, never overwritten
-- The health_checks table is current state only (upserted)
-- This table records every event for anomaly detection and trend analysis
CREATE TABLE IF NOT EXISTS health_check_history (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    subject_type TEXT NOT NULL,
    subject_key  TEXT NOT NULL,
    check_name   TEXT NOT NULL,
    status       TEXT NOT NULL,
    summary      TEXT NOT NULL DEFAULT '',
    checked_at   INTEGER NOT NULL DEFAULT (unixepoch())
);
CREATE INDEX IF NOT EXISTS idx_hch_key ON health_check_history (subject_key, check_name, checked_at);
CREATE INDEX IF NOT EXISTS idx_hch_status ON health_check_history (status, checked_at);

-- ---------------------------------------------------------------------------
-- Multi-provider tunnel instances
-- Unlike other slots, the tunnel slot supports multiple concurrent providers.
-- Each row is one active tunnel provider. Replaces/supplements infra_slots.tunnel
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS infra_tunnel_providers (
    id          INTEGER PRIMARY KEY,
    provider    TEXT NOT NULL UNIQUE,   -- cloudflared | tailscale | headscale
    status      TEXT NOT NULL DEFAULT 'empty'
                    CHECK (status IN ('empty', 'deploying', 'active', 'error', 'removed')),
    config      TEXT,                   -- JSON config (no secrets)
    deployed_at INTEGER,
    updated_at  INTEGER NOT NULL DEFAULT (unixepoch())
);

-- ---------------------------------------------------------------------------
-- LLM Model Registry — tracks active models and their task capabilities
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_model_registry (
    id              INTEGER PRIMARY KEY,
    filename        TEXT NOT NULL UNIQUE,   -- e.g. phi-4-mini-instruct-Q4_K_M.gguf
    display_name    TEXT,                   -- optional friendly name
    enabled         INTEGER NOT NULL DEFAULT 0,
    -- Capabilities: JSON array of task tags
    -- Built-in tags: reasoning, json, code, fast, general, classification
    capabilities    TEXT NOT NULL DEFAULT '[]',
    -- Per-task priority scores: JSON {task_type: 0.0-1.0}
    task_scores     TEXT NOT NULL DEFAULT '{}',
    -- Overall priority for tie-breaking (1=highest)
    priority        INTEGER NOT NULL DEFAULT 5,
    -- Context window in tokens (for routing large payloads)
    context_window  INTEGER NOT NULL DEFAULT 4096,
    -- Ollama model name (how it's registered in Ollama, may differ from filename)
    ollama_name     TEXT,
    -- Notes set by user
    notes           TEXT,
    registered_at   INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at      INTEGER NOT NULL DEFAULT (unixepoch())
);

-- ---------------------------------------------------------------------------
-- LLM routing log — one row per LLM call, which model handled which task
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS llm_routing_log (
    id          INTEGER PRIMARY KEY,
    ts          INTEGER NOT NULL DEFAULT (unixepoch()),
    app_key     TEXT NOT NULL,
    task_type   TEXT NOT NULL,          -- reasoning | json | fast | etc.
    model       TEXT NOT NULL,          -- model name used
    success     INTEGER NOT NULL,       -- 1=ok, 0=failed
    duration_ms INTEGER,               -- inference time
    error_type  TEXT,                  -- connection|timeout|parse|null
    summary     TEXT                   -- first 120 chars of LLM output
);
-- keep 30 days, prune on insert

-- ---------------------------------------------------------------------------
-- Maintenance windows — suppress anomaly alerts during known scheduled events
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS maintenance_windows (
    id          INTEGER PRIMARY KEY,
    app_key     TEXT NOT NULL,
    check_name  TEXT NOT NULL,
    label       TEXT NOT NULL DEFAULT 'Scheduled task',
    -- Recurrence: day_of_week 0=Mon…6=Sun, NULL=daily
    day_of_week INTEGER,
    -- Time window (UTC hour, inclusive)
    hour_start  INTEGER NOT NULL,
    hour_end    INTEGER NOT NULL DEFAULT -1,  -- -1 = hour_start+2
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  INTEGER NOT NULL DEFAULT (unixepoch())
);

-- ---------------------------------------------------------------------------
-- QuickStart wizard progress — persists phase completion across sessions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quickstart_phases (
    phase       TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'pending',
                -- pending | skipped | complete
    completed_at INTEGER
);

CREATE TABLE IF NOT EXISTS pending_fixes (
    id          INTEGER PRIMARY KEY,
    app_key     TEXT    NOT NULL,
    check_name  TEXT    NOT NULL,
    action_type TEXT    NOT NULL,
    problem     TEXT    NOT NULL,
    suggested_fix TEXT  NOT NULL,
    confidence  REAL    NOT NULL DEFAULT 0.5,
    status      TEXT    NOT NULL DEFAULT 'pending',
    model       TEXT,
    created_at  INTEGER NOT NULL DEFAULT (unixepoch()),
    resolved_at INTEGER,
    UNIQUE(app_key, check_name, action_type)
);