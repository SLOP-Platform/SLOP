-- Migration 007: Phase E safe auto-apply — fix_metadata column
--
-- Phase E of the install-monitor / self-healing agent.
-- Adds fix_metadata to pending_fixes so that apply.py can store
-- per-fix context (e.g. the image tag for repull_restart actions).
-- Rows inserted before Phase E default to the empty JSON object.
--
-- fix_type is derived at apply-time from diagnosis_class via
-- DIAGNOSIS_TO_FIX_TYPE in backend/agent/apply.py, so no column
-- is needed here.
--
-- SQLite supports ADD COLUMN without data loss.

ALTER TABLE pending_fixes ADD COLUMN fix_metadata TEXT NOT NULL DEFAULT '{}';
