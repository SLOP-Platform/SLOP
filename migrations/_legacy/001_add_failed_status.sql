-- Migration: add 'failed' to apps.status CHECK constraint
-- SQLite does not support ALTER TABLE ... ALTER COLUMN with new CHECK.
-- Recreate the table preserving all data.
-- Run once: sqlite3 /srv/slop/data/state.db < migrations/001_add_failed_status.sql

BEGIN;

CREATE TABLE apps_new (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    key             TEXT NOT NULL UNIQUE,
    display_name    TEXT NOT NULL,
    tier            INTEGER NOT NULL DEFAULT 2,
    category        TEXT NOT NULL DEFAULT 'tools',
    status          TEXT NOT NULL DEFAULT 'installing'
                        CHECK (status IN (
                            'installing', 'running', 'stopped',
                            'unhealthy', 'updating', 'removing', 'error', 'disabled',
                            'failed'
                        )),
    image           TEXT NOT NULL DEFAULT '',
    image_tag       TEXT NOT NULL DEFAULT 'latest',
    container_name  TEXT NOT NULL DEFAULT '',
    web_port        INTEGER,
    host_port       INTEGER,
    config_path     TEXT,
    manifest_source TEXT,
    manifest_hash   TEXT,
    extra_config    TEXT,
    installed_at    INTEGER,
    last_healthy_at INTEGER
);

INSERT INTO apps_new SELECT * FROM apps;
DROP TABLE apps;
ALTER TABLE apps_new RENAME TO apps;

COMMIT;
