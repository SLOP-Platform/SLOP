-- Migration 006: promote fix_history to pattern library
--
-- Phase C of the install-monitor / self-healing agent.
-- Adds diagnosis_class and signature_hash columns so that
-- classify_with_llm() can do an exact-hash cache hit before
-- calling the LLM (confidence=0.95 cache hit vs. 0.8 LLM call).
--
-- SQLite supports ADD COLUMN without data loss.

ALTER TABLE fix_history ADD COLUMN diagnosis_class TEXT NOT NULL DEFAULT 'UNKNOWN';
ALTER TABLE fix_history ADD COLUMN signature_hash TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_fix_history_class_app ON fix_history(diagnosis_class, app_key);
CREATE INDEX IF NOT EXISTS idx_fix_history_signature ON fix_history(signature_hash);
