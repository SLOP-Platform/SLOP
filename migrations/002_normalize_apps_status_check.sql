-- Migration 002: normalize apps.status CHECK constraint
--
-- v3 DBs may have an `apps` table whose status CHECK constraint pre-dates
-- the addition of the 'failed' state (see migrations/_legacy/001_add_failed_status.sql
-- which many operators never ran). This migration uses SQLite's table-recreate
-- dance to guarantee the canonical CHECK is in place.
--
-- Idempotent: safe to run on a DB whose apps table already has the canonical
-- CHECK. The DROP/RENAME dance ensures the constraint is correct regardless
-- of the pre-existing state.
--
-- Existing rows are preserved. FK references to apps.id are preserved because
-- the recreated table retains the same AUTOINCREMENT primary key sequence.
--
-- This file is IMMUTABLE after merge. Schema changes go in 003+.

PRAGMA foreign_keys = OFF;

BEGIN;

CREATE TABLE apps__mig002 (
    id              INTEGER PRIMARY KEY,
    key             TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    tier            INTEGER NOT NULL DEFAULT 2,
    category        TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'installing'
                        CHECK (status IN (
                            'installing', 'running', 'stopped',
                            'unhealthy', 'updating', 'removing', 'error', 'disabled',
                            'failed'
                        )),
    image           TEXT NOT NULL,
    image_tag       TEXT NOT NULL DEFAULT 'latest',
    container_name  TEXT NOT NULL,
    web_port        INTEGER,
    host_port       INTEGER,
    config_path     TEXT,
    manifest_source TEXT,
    manifest_hash   TEXT,
    extra_config    TEXT,
    installed_at    INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at      INTEGER NOT NULL DEFAULT (unixepoch()),
    last_healthy_at INTEGER
);

INSERT INTO apps__mig002 (
    id, key, display_name, tier, category, status,
    image, image_tag, container_name, web_port, host_port,
    config_path, manifest_source, manifest_hash, extra_config,
    installed_at, updated_at, last_healthy_at
)
SELECT
    id, key, display_name, tier, category, status,
    image, image_tag, container_name, web_port, host_port,
    config_path, manifest_source, manifest_hash, extra_config,
    installed_at, updated_at, last_healthy_at
FROM apps;

DROP TABLE apps;
ALTER TABLE apps__mig002 RENAME TO apps;

PRAGMA foreign_keys = ON;

COMMIT;
