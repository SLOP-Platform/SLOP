"""backend/api/audit.py — read-only audit log query endpoint (step 4.3.e).

Mounted at `/api/v1/audit` and `/api/audit` (legacy alias) via the
shared `_mount` helper. Returns rows from the `audit_log` table
reverse-chronologically; supports filtering by `actor`, `action`,
`resource_id`, and a `since=<unix_ts>` window.

The mutation surface — the writing of audit rows — lives in
`backend.api.middleware.AuditLogMiddleware`. This module is read-only;
there is no API for deleting or modifying past entries (per the
"immutable trail" contract from `docs/observability.md`).

Schema and rationale: see `migrations/004_audit_log.sql`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from backend.core.state import StateDB


router = APIRouter()


@router.get("")
def query_audit_log(
    since: int | None = Query(
        None,
        description="Unix epoch seconds — return only rows with ts >= since",
    ),
    actor: str | None = Query(None, description="Filter by actor"),
    action: str | None = Query(
        None,
        description="Filter by action template (e.g. 'POST /api/v1/apps/{key}/install')",
    ),
    resource_id: str | None = Query(None, description="Filter by resource id"),
    limit: int = Query(100, ge=1, le=1000, description="Max rows to return"),
) -> dict[str, Any]:
    """Return audit-log rows reverse-chronologically (newest first).

    All filters are AND-combined. `limit` is capped at 1000 rows to
    keep the API responsive — paginate via `since=<oldest_ts_seen>`
    for deeper history.
    """
    where: list[str] = []
    params: list[Any] = []
    if since is not None:
        where.append("ts >= ?")
        params.append(since)
    if actor is not None:
        where.append("actor = ?")
        params.append(actor)
    if action is not None:
        where.append("action = ?")
        params.append(action)
    if resource_id is not None:
        where.append("resource_id = ?")
        params.append(resource_id)
    sql = (
        "SELECT id, ts, actor, action, resource_id, request_body_hash, "
        "response_status, correlation_id FROM audit_log"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY ts DESC, id DESC LIMIT ?"
    params.append(limit)

    with StateDB() as db:
        rows = db.execute(sql, tuple(params)).fetchall()

    return {
        "rows": [dict(r) for r in rows],
        "count": len(rows),
    }
