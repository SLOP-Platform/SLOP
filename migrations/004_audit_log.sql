-- Migration 004 — audit_log table (step 4.3.d)
--
-- Immutable trail of mutation events. Every POST/PUT/DELETE/PATCH
-- request that hits the API surface writes one row here; the table
-- is append-only by convention (no UPDATE/DELETE statements in
-- backend code, only INSERT + SELECT).
--
-- Schema rationale:
--
--   actor               — single-tenant homelab today, always 'local'.
--                         Schema reserves the column for future multi-
--                         user mode (no migration needed when that ships).
--
--   action              — '<METHOD> <route_template>' e.g. 'POST /api/v1/apps/{key}/install'.
--                         Bounded cardinality (one entry per route × method).
--                         Route templates, NEVER literal URLs with IDs in them.
--
--   resource_id         — the path-parameter value(s) that identify the
--                         mutated resource. Concatenated with '/' if
--                         multiple. NULL when the request doesn't operate
--                         on a specific resource (e.g. POST /api/v1/apps/batch/install).
--
--   request_body_hash   — sha256 hex of the request body bytes. The full
--                         body is NOT stored: bodies often contain
--                         secrets (passkeys, tokens) that we must not
--                         persist. The hash supports non-repudiation
--                         (verifying a saved body matches the recorded
--                         action) without the storage / PII risk.
--
--   response_status     — HTTP status code returned. Failed requests
--                         (4xx/5xx) get audited too — the operator
--                         needs to see attempted mutations, not only
--                         completed ones.
--
--   correlation_id      — matches the X-Request-ID response header set
--                         by CorrelationIdMiddleware. Cross-correlates
--                         this audit row with the structured-log lines
--                         emitted during the same request.
--
-- Index strategy:
--
--   idx_audit_log_ts (DESC) supports the common query — "show me recent
--   activity, paged by time". DESC matters because the API endpoint
--   serves audit rows reverse-chronologically by default.
--
--   idx_audit_log_action supports filtering by action template, useful
--   for "show me every install attempt" / "show me every removal".
--
-- Retention: unlimited. Homelab volume is hundreds of rows per day max;
-- the table fits in memory for years. A future trim policy would live
-- in a separate maintenance migration if storage pressure ever appears.

-- IF NOT EXISTS for idempotency: the regenerated schema.sql includes
-- the audit_log table because that's what `tools/regenerate-schema-sql.py`
-- produces, and v3 DBs that load schema.sql then need migration 004 to
-- be a no-op rather than fail with "table already exists".
CREATE TABLE IF NOT EXISTS audit_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  INTEGER NOT NULL,
    actor               TEXT NOT NULL DEFAULT 'local',
    action              TEXT NOT NULL,
    resource_id         TEXT,
    request_body_hash   TEXT,
    response_status     INTEGER,
    correlation_id      TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_log_ts     ON audit_log(ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_action ON audit_log(action);
