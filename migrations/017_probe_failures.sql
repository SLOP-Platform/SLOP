-- migration 017: probe_failures table
-- Tracks health probes that have hit 5+ consecutive failures.
-- Written by the health scheduler; surfaced via GET /api/health/probe-failures.

CREATE TABLE IF NOT EXISTS probe_failures (
    probe_name      TEXT PRIMARY KEY,
    fail_count      INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    last_failed_at  INTEGER
);
