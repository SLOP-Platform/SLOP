-- Migration 015: llm_routing_log table
-- Extracted from backend/health/checker_llm.py ad-hoc CREATE TABLE
CREATE TABLE IF NOT EXISTS llm_routing_log (
    id INTEGER PRIMARY KEY,
    ts INTEGER NOT NULL DEFAULT (unixepoch()),
    app_key TEXT NOT NULL, task_type TEXT NOT NULL,
    model TEXT NOT NULL, success INTEGER NOT NULL,
    duration_ms INTEGER, error_type TEXT, summary TEXT
);
