-- Migration 016: pending_fixes table
-- Extracted from backend/health/checker_llm.py ad-hoc CREATE TABLE
CREATE TABLE IF NOT EXISTS pending_fixes (
    id           INTEGER PRIMARY KEY,
    app_key      TEXT NOT NULL,
    check_name   TEXT NOT NULL,
    action_type  TEXT NOT NULL,
    problem      TEXT NOT NULL,
    suggested_fix TEXT NOT NULL,
    confidence   REAL NOT NULL DEFAULT 0.5,
    status       TEXT NOT NULL DEFAULT 'pending',
    model        TEXT,
    created_at   INTEGER NOT NULL DEFAULT (unixepoch()),
    resolved_at  INTEGER,
    UNIQUE(app_key, check_name, action_type)
);
