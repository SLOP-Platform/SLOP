-- Migration 020: referential link pending_fixes -> fix_history (#822 Unit B)
--
-- Unit A made every fix outcome carry a signature_hash via the single
-- record_fix_outcome seam. Unit B adds the EXPLICIT referential link so a
-- pending_fixes row records WHICH fix_history row it produced. This replaces the
-- fragile (app_key, MAX(created_at)) heuristic the post-fix verification used to
-- pick a row to stamp (which updates the WRONG row whenever more than one
-- fix_history row exists for an app).
--
-- SQLite cannot add a column-level REFERENCES via ALTER TABLE; the link is an
-- INTEGER fix_history.id, NULL until the row's outcome is recorded. SQLite
-- supports ADD COLUMN without data loss.

ALTER TABLE pending_fixes ADD COLUMN fix_history_id INTEGER;
