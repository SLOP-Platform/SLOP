-- Migration 011: extend operations.triggered_by CHECK to include 'agent'
--
-- The baseline CHECK only allowed ('user', 'cli', 'health', 'scheduler').
-- This migration adds autonomous pre-execution action logging from the agent
-- pipeline (self_heal + auto_apply paths), which requires a new trigger
-- source 'agent' distinct from the existing scheduler/health labels.
--
-- Uses SQLite's table-recreate dance (same pattern as migration 002).
-- Existing rows are preserved via INSERT … SELECT. Indexes are recreated.
--
-- This file is IMMUTABLE after merge. Schema changes go in 012+.

PRAGMA foreign_keys = OFF;

BEGIN;

CREATE TABLE operations__mig011 (
    id              INTEGER PRIMARY KEY,
    operation       TEXT NOT NULL,
    subject_type    TEXT NOT NULL,
    subject_key     TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('started', 'completed', 'failed', 'rolled_back')),
    triggered_by    TEXT NOT NULL DEFAULT 'user'
                        CHECK (triggered_by IN ('user', 'cli', 'health', 'scheduler', 'agent')),
    detail          TEXT,
    error           TEXT,
    started_at      INTEGER NOT NULL DEFAULT (unixepoch()),
    completed_at    INTEGER
);

INSERT INTO operations__mig011 (
    id, operation, subject_type, subject_key, status,
    triggered_by, detail, error, started_at, completed_at
)
SELECT
    id, operation, subject_type, subject_key, status,
    triggered_by, detail, error, started_at, completed_at
FROM operations;

DROP TABLE operations;
ALTER TABLE operations__mig011 RENAME TO operations;

CREATE INDEX IF NOT EXISTS idx_operations_subject ON operations(subject_type, subject_key);
CREATE INDEX IF NOT EXISTS idx_operations_started  ON operations(started_at DESC);

PRAGMA foreign_keys = ON;

COMMIT;
