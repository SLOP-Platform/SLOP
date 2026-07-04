-- Migration 021: drop orphaned router_decisions table (#1130, phase-2 of #1090)
--
-- #1090 removed the dead router_decisions DB write (backend/agent/router/
-- decisions.py) and fixed the prompt_chars bug. The table is now provably
-- orphaned: 0 writers, 0 readers (the only remaining reference is a comment in
-- decisions.py documenting the removal). This migration drops it and its two
-- indexes so the schema no longer carries a dead table.
--
-- Safe/irreversible-by-design: a DROP of an unwritten/unread table loses no
-- live data. Existing installs simply shed the empty table on next migrate.

BEGIN;

DROP INDEX IF EXISTS idx_router_decisions_created_at;
DROP INDEX IF EXISTS idx_router_decisions_chosen_provider;
DROP TABLE IF EXISTS router_decisions;

COMMIT;
