"""backend/health/managed_services.py

Health checks for managed services (postgres, redis/valkey).
Runs on every health cycle. If a managed service is unhealthy,
all dependent apps are marked with a shared root-cause warning
instead of generating individual per-app LLM diagnoses.
"""

from __future__ import annotations

from typing import Any

from backend.core.logging import get_logger
from backend.health.swallow_counter import record_swallow

import subprocess
import time

log = get_logger(__name__)

_MANAGED = {
    "postgres": {
        "ready_cmd": ["docker", "exec", "postgres", "pg_isready", "-U", "slop", "-q"],
        "display": "PostgreSQL",
    },
    "redis": {
        "ready_cmd": ["docker", "exec", "redis", "valkey-cli", "PING"],
        "display": "Redis/Valkey",
    },
}


def check_managed_services() -> dict[str, dict[str, Any]]:
    """Check health of all running managed services.

    Returns:
        { "postgres": {"healthy": bool, "message": str}, ... }
    """
    from backend.core.state import StateDB
    from backend.core import docker_client as _dc

    results: dict[str, dict[str, Any]] = {}

    with StateDB() as db:
        running_managed = {
            row["key"]: row
            for row in db.execute(
                "SELECT key, status FROM apps WHERE key IN ('postgres','redis') "
                "AND status NOT IN ('disabled','removing','failed')"
            ).fetchall()
        }

    for svc_key, cfg in _MANAGED.items():
        if svc_key not in running_managed:
            continue  # not installed — skip

        # 1. Is the container running at all?
        container = _dc.get_container(svc_key)
        if not container or container.status != "running":
            results[svc_key] = {
                "healthy": False,
                "message": (
                    f"{cfg['display']} container is not running. Run: docker start {svc_key}"
                ),
            }
            # Update DB status
            try:
                with StateDB() as db:
                    db.upsert_app(svc_key, status="error")
                    db.execute(
                        "INSERT OR REPLACE INTO managed_services "
                        "(service_type, status, last_checked) VALUES (?, 'error', ?)",
                        (svc_key, int(time.time())),
                    )
            except Exception:  # best-effort status DB write; continue to next service
                record_swallow("managed_services.container_status_write")
            continue

        # 2. Is the service actually accepting connections?
        try:
            r = subprocess.run(
                cfg["ready_cmd"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            healthy = r.returncode == 0
            if svc_key == "redis":
                healthy = healthy and "PONG" in r.stdout
        except Exception as e:
            healthy = False
            log.debug("Managed service check failed for %s: %s", svc_key, e)

        msg = (
            f"{cfg['display']} is healthy"
            if healthy
            else f"{cfg['display']} is running but not accepting connections. "
            f"Check: docker logs {svc_key}"
        )
        results[svc_key] = {"healthy": healthy, "message": msg}

        # Update managed_services table
        try:
            with StateDB() as db:
                db.execute("""
                    CREATE TABLE IF NOT EXISTS managed_services (
                        service_type TEXT PRIMARY KEY,
                        status TEXT NOT NULL DEFAULT 'unknown',
                        last_checked INTEGER
                    )""")
                db.execute(
                    "INSERT OR REPLACE INTO managed_services "
                    "(service_type, status, last_checked) VALUES (?, ?, ?)",
                    (svc_key, "running" if healthy else "error", int(time.time())),
                )
        except Exception:  # best-effort managed_services table write; table may not exist yet
            record_swallow("managed_services.table_write")

    return results


def get_unhealthy_managed() -> list[str]:
    """Return service keys that are currently unhealthy."""
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            rows = db.execute(
                "SELECT service_type FROM managed_services WHERE status='error'"
            ).fetchall()
        return [r["service_type"] for r in rows]
    except Exception:
        return []
