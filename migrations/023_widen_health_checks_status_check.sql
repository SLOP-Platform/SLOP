-- Migration 023: widen health_checks.status CHECK constraint
--
-- The original health_checks CHECK allowed only ('ok','warning','error','unknown'),
-- but the agent subsystem legitimately writes a richer vocabulary to this table:
--
--   * 'critical' / 'degraded' — backend/agent/spine.py VERDICT_TO_HEALTH_STATUS
--     (Verdict.DRIFT -> 'critical', Verdict.INCONSISTENT -> 'degraded') and
--     backend/health/checker.py integrity_status (critical_gaps -> 'critical',
--     high_gaps -> 'degraded'). The frontend (HealthView.vue) keys the integrity
--     panel border on status==='critical' / ==='degraded'.
--   * 'running' / 'disabled' — backend/core/agent.py _write_agent_health
--     (the agent_status check reports operational state).
--   * 'skipped' — backend/manifests/executor.py step.status passthrough.
--
-- Under the old CHECK every such write raised IntegrityError, was caught-and-
-- logged ("Failed to write agent/process_integrity health check", "self-audit
-- persist failed"), and the row was SILENTLY DROPPED. Net effect: the agent
-- self-audit could never persist a DRIFT/INCONSISTENT finding (the KL keystone
-- "a green light must be able to go RED" — the red literally could not be
-- written), the integrity UI panel was permanently blank, and agent_status was
-- stuck at its baseline 'unknown'. See backlog #1309 for the full GROUND repro.
--
-- This migration uses SQLite's table-recreate dance (SQLite cannot ALTER a CHECK
-- in place) to install the canonical widened CHECK. Existing rows are preserved;
-- the UNIQUE(subject_type, subject_key, check_name) constraint is retained.
--
-- Idempotent: safe to run on a DB whose health_checks table already has the
-- canonical CHECK. The DROP/RENAME dance ensures the constraint is correct
-- regardless of the pre-existing state.
--
-- This file is IMMUTABLE after merge. Schema changes go in 024+.

PRAGMA foreign_keys = OFF;

BEGIN;

CREATE TABLE health_checks__mig023 (
    id              INTEGER PRIMARY KEY,
    subject_type    TEXT NOT NULL,
    subject_key     TEXT NOT NULL,
    check_name      TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN (
                        'ok', 'warning', 'error', 'unknown',
                        'critical', 'degraded', 'running', 'disabled', 'skipped'
                    )),
    summary         TEXT NOT NULL,
    detail          TEXT,
    auto_fix        TEXT,
    checked_at      INTEGER NOT NULL DEFAULT (unixepoch()),
    UNIQUE (subject_type, subject_key, check_name)
);

INSERT INTO health_checks__mig023 (
    id, subject_type, subject_key, check_name, status,
    summary, detail, auto_fix, checked_at
)
SELECT
    id, subject_type, subject_key, check_name, status,
    summary, detail, auto_fix, checked_at
FROM health_checks;

DROP TABLE health_checks;
ALTER TABLE health_checks__mig023 RENAME TO health_checks;

PRAGMA foreign_keys = ON;

COMMIT;
