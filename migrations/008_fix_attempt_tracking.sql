-- Migration 008: restart-oscillation guard — fix_attempts table
--
-- Backoff primitives for the SLOP Agent safe-apply tier.
-- Adds fix_attempts to record each auto-fix attempt so that backoff.py
-- can deny further attempts when a container oscillates (>= max_attempts
-- within a rolling window, or when the exponential backoff interval has
-- not yet elapsed since the last attempt).
--
-- Rows are append-only: every call to record_attempt() inserts a new row.
-- The backoff logic reads the last N rows for (app_key, fix_type) filtered
-- by created_at >= (now - window_s) using the covering index below.
--
-- outcome values: 'success', 'failed_verification', 'error'

BEGIN;

CREATE TABLE IF NOT EXISTS fix_attempts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    app_key    TEXT    NOT NULL,
    fix_type   TEXT    NOT NULL,
    outcome    TEXT    NOT NULL,
    created_at INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_fix_attempts_app_fix_time
    ON fix_attempts (app_key, fix_type, created_at);

COMMIT;
