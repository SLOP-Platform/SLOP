"""migrations/003_sync_legacy_tunnel_slot.py

Migration 003: lift the inline tunnel-slot sync out of init_db().

Context: state.py:init_db() contained ad-hoc code that copied an active
cloudflared provider from infra_slots['tunnel'] into infra_tunnel_providers
and then cleared the slot. That code ran on every startup. This migration
runs it exactly once via the proper migration framework.

v3 DBs: the active tunnel (if any) is moved to infra_tunnel_providers.
Fresh DBs: the infra_tunnel_providers table exists but is empty — no-op.
Idempotent: if the tunnel was already migrated (row exists), does nothing.
"""
from __future__ import annotations

import sqlite3


def upgrade(conn: sqlite3.Connection) -> None:
    """Copy the active tunnel slot provider into infra_tunnel_providers."""
    row = conn.execute(
        "SELECT provider, status, config, deployed_at "
        "FROM infra_slots "
        "WHERE slot = 'tunnel' AND provider IS NOT NULL AND status = 'active'"
    ).fetchone()

    if row is None:
        # No active tunnel in infra_slots — nothing to migrate.
        return

    provider, status, config, deployed_at = (
        row["provider"], row["status"], row["config"], row["deployed_at"]
    )

    existing = conn.execute(
        "SELECT id FROM infra_tunnel_providers WHERE provider = ?",
        (provider,),
    ).fetchone()

    if existing is None:
        conn.execute(
            "INSERT INTO infra_tunnel_providers "
            "(provider, status, config, deployed_at, updated_at) "
            "VALUES (?, ?, ?, ?, unixepoch())",
            (provider, status, config, deployed_at),
        )

    # Clear the legacy tunnel slot — it is now managed by infra_tunnel_providers.
    conn.execute(
        "UPDATE infra_slots "
        "SET provider = NULL, status = 'empty', config = NULL "
        "WHERE slot = 'tunnel'"
    )
