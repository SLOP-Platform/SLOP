-- Migration 018: evidence-ranked learning — image_digest plumbing + shadow log
--
-- Replaces the flat-0.95 confidence cache
-- (checker_llm.py / classifier.py) with an OUTCOME-WEIGHTED score keyed on the
-- content-addressed image_digest (not the mutable image tag), so a fix learned
-- on one image version is not blindly replayed across a different version.
--
-- Adds:
--   1. fix_history.image_digest   — the content-addressed digest of the image the
--      app was running when the fix was recorded. NULL/'' for legacy rows (the
--      scorer treats absent digest as "version-blind, lower confidence").
--   2. fix_attempts.image_digest  — same, for the backoff/attempt log.
--   3. idx_fix_history_sig_digest  — covering index for the outcome-weighted read
--      (signature_hash, image_digest, outcome, created_at).
--   4. learning_shadow_log         — the SHADOW GATE substrate. Every learning
--      lookup logs {learned score, the legacy flat-0.95 it would have used,
--      whether they agreed, the subsequent outcome}. The scorer runs in shadow
--      (logged, not enforced) until this log proves it beats the flat cache; the
--      `agent_learning_shadow` setting flag governs enforcement.
--
-- SQLite supports ADD COLUMN without data loss; the migration runner skips
-- duplicate-column ALTERs idempotently.

BEGIN;

ALTER TABLE fix_history  ADD COLUMN image_digest TEXT NOT NULL DEFAULT '';
ALTER TABLE fix_attempts ADD COLUMN image_digest TEXT NOT NULL DEFAULT '';

-- Covering index for the outcome-weighted score read: group by signature_hash
-- (+ digest) and tally outcomes within a recency window.
CREATE INDEX IF NOT EXISTS idx_fix_history_sig_digest
    ON fix_history (signature_hash, image_digest, outcome, created_at);

-- Shadow gate log: proves the new scoring is non-vacuous (it changes with
-- outcomes) before it is allowed to influence behaviour.
CREATE TABLE IF NOT EXISTS learning_shadow_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    app_key         TEXT    NOT NULL,
    signature_hash  TEXT    NOT NULL DEFAULT '',
    image_digest    TEXT    NOT NULL DEFAULT '',
    learned_score   REAL    NOT NULL,           -- derived, outcome-weighted
    legacy_score    REAL    NOT NULL,           -- the flat 0.95 it replaces
    sample_size     INTEGER NOT NULL DEFAULT 0, -- evidence count behind the score
    success_count   INTEGER NOT NULL DEFAULT 0,
    failure_count   INTEGER NOT NULL DEFAULT 0,
    digest_match    INTEGER NOT NULL DEFAULT 0, -- 1 iff evidence shares this digest
    enforced        INTEGER NOT NULL DEFAULT 0, -- 1 iff learned score drove behaviour
    created_at      INTEGER NOT NULL DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_learning_shadow_sig
    ON learning_shadow_log (signature_hash, created_at);

COMMIT;
