-- Migration 005: LLM agent — add diagnosis_class to pending_fixes
--
-- Phase A of the install-monitor / self-healing agent.
-- Existing rows default to 'UNKNOWN' (Phase B will backfill with regex
-- classifier; Phase C will fill from LLM diagnosis).
--
-- SQLite supports ADD COLUMN without data loss.

ALTER TABLE pending_fixes ADD COLUMN diagnosis_class TEXT NOT NULL DEFAULT 'UNKNOWN';
