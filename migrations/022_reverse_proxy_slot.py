"""migrations/022_reverse_proxy_slot.py

Migration 022: admit the ``reverse_proxy`` slot into the ``infra_slots`` table.

Context (#990): ``reverse_proxy`` is the new FOUNDATIONAL infra slot (Traefik is its
first provider — other slots emit their route/forwardauth labels through it). It is a
deployable (non-selection) slot, so ``slots.deployable_slots()`` now includes it, which
means it must have an ``infra_slots`` DB row and pass the table's slot CHECK.

The baseline ``infra_slots`` CHECK only admits the original five slots
(``'auth','tunnel','vpn','management','dashboard'`` — schema.sql:168 /
001_baseline.sql:62). SQLite cannot ``ALTER`` a CHECK constraint in place, so this
migration does the canonical table-rebuild: create a new table whose slot CHECK is
extended with ``'reverse_proxy'``, copy every existing row across, drop the old table,
rename the new one into place, then seed the ``reverse_proxy`` row.

Idempotent: if the live ``infra_slots`` CHECK already admits ``reverse_proxy`` (re-run
or fresh DB already migrated) the rebuild is skipped and only the seed (``INSERT OR
IGNORE``) runs. Reversible-class table-rebuild — no FK references ``infra_slots`` (grep
confirms none), so the drop/rename is self-contained.
"""

from __future__ import annotations

import sqlite3

# The rebuilt table — identical to schema.sql:165-175 EXCEPT the slot CHECK is
# extended with 'reverse_proxy'. Kept column-for-column in sync with schema.sql so a
# fresh DB (schema.sql/001 → 022) and an upgraded DB converge on the same shape.
# Multi-line so the regenerated schema.sql (which dumps SQLite's stored CREATE text
# verbatim) stays human-readable. Compact indentation keeps every line ≤100 cols.
_CREATE_NEW = """\
CREATE TABLE infra_slots_new (
    id INTEGER PRIMARY KEY,
    slot TEXT NOT NULL UNIQUE
        CHECK (slot IN ('auth', 'tunnel', 'vpn', 'management', 'dashboard', 'reverse_proxy')),
    provider TEXT,
    status TEXT NOT NULL DEFAULT 'empty'
        CHECK (status IN ('empty', 'deploying', 'active', 'migrating', 'error', 'removed')),
    config TEXT,
    deployed_at INTEGER,
    updated_at INTEGER NOT NULL DEFAULT (unixepoch())
)"""


def upgrade(conn: sqlite3.Connection) -> None:
    """Extend the infra_slots slot CHECK to admit 'reverse_proxy' and seed its row.

    Uses discrete statements (not executescript, which would COMMIT the runner's
    BEGIN IMMEDIATE mid-migration) so the rebuild+seed stays atomic within the
    migration framework's transaction.
    """
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='infra_slots'"
    ).fetchone()
    table_sql = (row[0] if row else "") or ""

    # Only rebuild when the live CHECK does not already admit reverse_proxy (re-run /
    # already-migrated DB → skip straight to the idempotent seed).
    if "reverse_proxy" not in table_sql:
        conn.execute(_CREATE_NEW)
        conn.execute(
            "INSERT INTO infra_slots_new "
            "(id, slot, provider, status, config, deployed_at, updated_at) "
            "SELECT id, slot, provider, status, config, deployed_at, updated_at "
            "FROM infra_slots"
        )
        conn.execute("DROP TABLE infra_slots")
        conn.execute("ALTER TABLE infra_slots_new RENAME TO infra_slots")

    # Seed the slot row (INSERT OR IGNORE → a fresh seed is safe and a re-run a no-op).
    conn.execute("INSERT OR IGNORE INTO infra_slots (slot) VALUES ('reverse_proxy')")
