"""backend/api/operations.py — read-only operations log query endpoint (#733).

Mounted at `/api/v1/operations` and `/api/operations` (legacy alias) via the
shared `_mount` helper in main.py. Returns rows from the `operations` table
reverse-chronologically by started_at; supports filtering by `since`,
`subject_key`, `triggered_by`, and `status`.

The write surface — logging operation rows — lives in the executor and agent
pipeline. This module is read-only; no mutation endpoints are exposed here.

Schema: see migrations/001_baseline.sql (table definition) and
migrations/011_operations_triggered_by_agent.sql (triggered_by 'agent').
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from backend.core.state import StateDB


router = APIRouter()


@router.get("")
def query_operations(
    since: int | None = Query(
        None,
        description="Unix epoch seconds — return only rows with started_at >= since",
    ),
    subject_key: str | None = Query(None, description="Filter by subject key (e.g. app name)"),
    triggered_by: str | None = Query(
        None,
        description="Filter by trigger source: user | cli | health | scheduler | agent",
    ),
    status: str | None = Query(
        None,
        description="Filter by status: started | completed | failed | rolled_back",
    ),
    limit: int = Query(100, ge=1, le=500, description="Max rows to return (default 100, max 500)"),
) -> dict[str, Any]:
    """Return operations rows reverse-chronologically (newest first).

    All filters are AND-combined. `limit` is capped at 500 rows to
    keep the API responsive — paginate via `since=<oldest_started_at_seen>`
    for deeper history.
    """
    where: list[str] = []
    params: list[Any] = []
    if since is not None:
        where.append("started_at >= ?")
        params.append(since)
    if subject_key is not None:
        where.append("subject_key = ?")
        params.append(subject_key)
    if triggered_by is not None:
        where.append("triggered_by = ?")
        params.append(triggered_by)
    if status is not None:
        where.append("status = ?")
        params.append(status)
    sql = (
        "SELECT id, operation, subject_type, subject_key, status, "
        "triggered_by, detail, error, started_at, completed_at FROM operations"
    )
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY started_at DESC, id DESC LIMIT ?"
    params.append(limit)

    with StateDB() as db:
        rows = db.execute(sql, tuple(params)).fetchall()

    return {
        "rows": [dict(r) for r in rows],
        "count": len(rows),
    }
