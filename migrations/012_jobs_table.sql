-- Migration 012: jobs — persist async wizard / Ollama-setup job state.
--
-- Background:
--   The platform wizard ("run-async") and the Ollama setup flow each spawn a
--   background thread and hand the frontend a job_id to poll. Job state used to
--   live only in process-memory dicts (_wizard_jobs / _ollama_jobs), so any
--   backend restart orphaned polling clients: they kept polling a job_id that
--   no longer existed and got a 404 / timeout with no explanation.
--
--   This table persists job state so polling survives a restart. The in-memory
--   dicts remain the hot path (the running thread mutates them); every mutation
--   is mirrored to this table, and the polling endpoints fall back to the table
--   when the in-memory entry is absent (i.e. after a restart).
--
-- Restart semantics:
--   A job whose row is still status='running' when the process restarts cannot
--   be resumed (its thread is gone). On startup such rows are flipped to
--   status='unknown' — NOT 'error' and NOT auto-restarted. The frontend renders
--   "unknown" as an indeterminate state the operator can retry from.
--
-- Fields:
--   id          — the job_id (uuid4 string) handed to the polling client
--   kind        — 'wizard' | 'ollama' (which flow produced the job)
--   status      — 'running' | 'done' | 'error' | 'unknown'
--   payload     — full job dict as JSON; the exact shape the polling endpoint
--                 returns (kept opaque here so the HTTP API can evolve without a
--                 migration). Wizard: {steps, done, platform_ready, error, …};
--                 Ollama: {model, phase, progress, message, done, ok, …}.
--   created_at  — Unix timestamp (seconds) the job was created
--   updated_at  — Unix timestamp (seconds) of the last mutation

BEGIN;

CREATE TABLE IF NOT EXISTS jobs (
    id          TEXT    PRIMARY KEY,
    kind        TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'running',
    payload     TEXT    NOT NULL DEFAULT '{}',   -- JSON
    created_at  INTEGER NOT NULL DEFAULT (unixepoch()),
    updated_at  INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status);

CREATE INDEX IF NOT EXISTS idx_jobs_kind ON jobs (kind);

COMMIT;
