#!/usr/bin/env python3
"""backend/scripts/self_heal.py

Self-heal operations run by ms-update after every service restart.
Cleans up data inconsistencies that accumulate between deployments:

  1. Orphaned DB records — apps with no compose fragment (removed externally)
  2. Ghost compose fragments — fragments with no DB record
  3. Stale health records — health data for apps no longer in DB
  4. History pruning — keeps last 500 rows per app/check combination

This was previously embedded as a shell heredoc in ms-update.
Extracted to a .py file so it is scanned by Ruff, Bandit, and Semgrep.

Called by ms-update as:
  python3 backend/scripts/self_heal.py REPO
"""

import pathlib
import sqlite3 as _sq
import sys

# Infra-managed apps — these run without standard compose fragments
INFRA = {
    "traefik",
    "tinyauth",
    "authelia",
    "cloudflared",
    "tailscale",
    "headscale",
    "gluetun",
    "glance",
    "homepage",
    "dockge",
    "dockhand",
    "komodo",
    "portainer",
    "portainer_be",
}

G = "\033[32m"
R = "\033[0m"


def main() -> None:
    repo = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else pathlib.Path(".")
    db_path = repo / "data" / "state.db"
    compose_dir = repo / "data" / "compose"

    if not db_path.exists():
        sys.exit(0)

    conn = _sq.connect(str(db_path))
    conn.row_factory = _sq.Row

    # ── 1. Orphaned DB records ────────────────────────────────────────────────
    # App in DB with status=running/installed but no compose fragment → remove
    if compose_dir.exists():
        ACTIVE = ("running", "installed", "active", "degraded", "failed")
        rows = conn.execute(
            "SELECT key FROM apps WHERE status IN ({})".format(  # noqa: S608  # nosec B608  # IN clause built from constant tuple; values bound via ?
                ",".join("?" * len(ACTIVE))
            ),
            ACTIVE,
        ).fetchall()

        for row in rows:
            key = row[0]
            if key in INFRA:
                continue
            frag = compose_dir / f"{key}.yaml"
            if not frag.exists():
                conn.execute("DELETE FROM apps WHERE key=?", (key,))
                conn.execute("DELETE FROM health_checks WHERE subject_key=?", (key,))
                conn.execute("DELETE FROM health_check_history WHERE subject_key=?", (key,))
                conn.execute("DELETE FROM operations WHERE subject_key=?", (key,))
                try:
                    conn.execute("DELETE FROM pending_fixes WHERE app_key=?", (key,))
                except Exception:  # noqa: S110  # pending_fixes table may not exist on older DBs
                    pass
                print(f"  {G}+{R} Auto-removed orphaned record: '{key}'")

        conn.commit()

        # ── 2. Ghost compose fragments ────────────────────────────────────────
        # Fragment exists but no DB record → remove stale fragment
        installed = {r[0] for r in conn.execute("SELECT key FROM apps")}
        for frag in sorted(compose_dir.glob("*.yaml")):
            key = frag.stem
            if key in INFRA or key in installed:
                continue
            frag.unlink()
            print(f"  {G}+{R} Removed ghost fragment: {frag.name}")

    # ── 3. Stale health records ───────────────────────────────────────────────
    # Health check data for apps no longer in DB → delete
    all_keys = {r[0] for r in conn.execute("SELECT key FROM apps")}
    stale = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT subject_key FROM health_checks WHERE subject_type='app'"
        )
        if r[0] not in all_keys
    ]
    if stale:
        conn.execute(
            "DELETE FROM health_checks WHERE subject_type='app' "
            "AND subject_key NOT IN (SELECT key FROM apps)"
        )
        conn.execute(
            "DELETE FROM health_check_history WHERE subject_type='app' "
            "AND subject_key NOT IN (SELECT key FROM apps)"
        )
        conn.commit()
        print(f"  {G}+{R} Cleared stale health records: {', '.join(stale)}")

    # ── 4. History pruning ────────────────────────────────────────────────────
    # Keep only the 500 most recent rows per app/check combination
    conn.execute(
        "DELETE FROM health_check_history WHERE rowid NOT IN ("
        "SELECT rowid FROM health_check_history h2 "
        "WHERE h2.subject_key = health_check_history.subject_key "
        "AND h2.check_name = health_check_history.check_name "
        "ORDER BY checked_at DESC LIMIT 500)"
    )
    pruned = conn.total_changes
    if pruned > 0:
        print(f"  {G}+{R} Pruned {pruned} old health history rows")

    conn.commit()
    conn.close()


if __name__ == "__main__":
    main()
