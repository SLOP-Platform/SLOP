# migrations/

Database migration files for SLOP v4.

## Overview

Each migration is a numbered file: `NNN_short_description.sql` or
`NNN_short_description.py`. The runner (`backend/core/migrations.py`)
applies all pending migrations in numeric order on every startup. It is
idempotent: calling it twice is always a no-op the second time.

See the design doc: [`docs/cleanup/01_migrations_design.md`](../docs/cleanup/01_migrations_design.md)

## Current migrations

| # | File | Type | Purpose |
|---|---|---|---|
| 001 | `001_baseline.sql` | SQL | Byte-copy of schema.sql at v4 launch. Stamped (not re-run) on v3 DBs. **Immutable.** |
| 002 | `002_normalize_apps_status_check.sql` | SQL | Idempotent recreate of `apps` to guarantee `status` CHECK includes `'failed'`. |
| 003 | `003_sync_legacy_tunnel_slot.py` | Python | Moves active tunnel from `infra_slots` to `infra_tunnel_providers`. Replaces inline init_db() code. |

## Adding a new migration

1. Pick the next version number: `max(existing) + 1`.
2. Create the file: `migrations/NNN_short_description.sql` (or `.py`).
3. For SQL: use `BEGIN; ... COMMIT;` blocks. The runner uses `executescript()`.
4. For Python: export `def upgrade(conn: sqlite3.Connection) -> None`.
5. Test it: `pytest tests/test_migrations.py`
6. Regenerate schema.sql: `python3 tools/regenerate-schema-sql.py`
7. Commit the migration **and** the updated schema.sql in the same commit.
8. Run CI checks — must be clean.

**Migration files are immutable after merge.** The checksum recorded in
`schema_migrations` at apply time is verified on every subsequent startup.
Editing a committed migration is a startup failure (Core Rule 6.1).

## `_legacy/`

Pre-v4 hand-run scripts. Preserved for reference. The runner ignores this
directory (underscore-prefixed directories are skipped by the file scanner).
