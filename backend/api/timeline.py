"""backend/api/timeline.py — unified event timeline endpoint (#778).

Mounted at `/api/v1/timeline` and `/api/timeline` (legacy alias) via the
shared `_mount` helper in main.py. Returns a reverse-chronological merged
stream from four source tables: audit_log, operations, health_check_history,
and cloud_llm_usage.

All sources are UNION ALL'd in SQL; filtering by `since`, `types`, and `limit`
is applied per-branch before the outer ORDER BY so SQLite can use the
individual table indexes.

Schema references:
  audit_log            — migrations/004_audit_log.sql
  operations           — migrations/001_baseline.sql
  health_check_history — migrations/001_baseline.sql
  cloud_llm_usage      — migrations/001_baseline.sql
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from backend.core.state import StateDB

router = APIRouter()

_ALL_TYPES = {"api_mutation", "operation", "health_check", "llm_call"}


def _build_summary(row: dict[str, Any]) -> str:
    """Build the human-readable summary string for a timeline event row."""
    t = row["type"]
    if t == "api_mutation":
        return f'"{row["action"]}" → {row["response_status"]}'
    if t == "operation":
        return (
            f'"{row["operation"]} {row["subject_key"]}'
            f" — {row['status']}"
            f' (triggered by {row["triggered_by"]})"'
        )
    if t == "health_check":
        return f"{row['subject_key']} {row['check_name']}: {row['status']}"
    if t == "llm_call":
        cost = row["cost_usd"] if row["cost_usd"] is not None else 0.0
        return f"{row['provider']}/{row['model']} — {row['purpose']} (${cost:.4f})"
    return ""


def _build_detail(row: dict[str, Any]) -> dict[str, Any]:
    """Extract the per-type detail dict from a raw row."""
    t = row["type"]
    if t == "api_mutation":
        return {
            "action": row["action"],
            "resource_id": row["resource_id"],
            "response_status": row["response_status"],
            "actor": row["actor"],
            "correlation_id": row["correlation_id"],
        }
    if t == "operation":
        return {
            "operation": row["operation"],
            "subject_key": row["subject_key"],
            "subject_type": row["subject_type"],
            "status": row["status"],
            "triggered_by": row["triggered_by"],
            "error": row["error"],
        }
    if t == "health_check":
        return {
            "subject_type": row["subject_type"],
            "subject_key": row["subject_key"],
            "check_name": row["check_name"],
            "status": row["status"],
            "summary": row["hch_summary"],
        }
    if t == "llm_call":
        return {
            "provider": row["provider"],
            "model": row["model"],
            "purpose": row["purpose"],
            "cost_usd": row["cost_usd"],
            "prompt_tokens": row["prompt_tokens"],
            "completion_tokens": row["completion_tokens"],
            "sanitized": bool(row["sanitized"]),
        }
    return {}


@router.get("")
def query_timeline(
    since: int | None = Query(
        None,
        description="Unix epoch seconds — return only events with ts > since",
    ),
    types: str | None = Query(
        None,
        description=("Comma-separated filter: api_mutation,operation,health_check,llm_call"),
    ),
    limit: int = Query(
        200, ge=1, le=1000, description="Max events to return (default 200, max 1000)"
    ),
) -> dict[str, Any]:
    """Return a unified reverse-chronological event stream.

    Merges rows from audit_log (api_mutation), operations (operation),
    health_check_history (health_check), and cloud_llm_usage (llm_call).
    All filters are applied per-branch in the UNION ALL before the outer
    ORDER BY so SQLite can leverage the per-table indexes.
    """
    # Resolve which types to include
    if types is not None:
        requested = {t.strip() for t in types.split(",") if t.strip()}
        active_types = _ALL_TYPES & requested
    else:
        active_types = _ALL_TYPES

    branches: list[str] = []
    params: list[Any] = []

    # All SQL branch strings below are composed entirely from hardcoded
    # column names and table names; no user input is ever interpolated into
    # the SQL text (user-supplied values travel through `params`).  The
    # # nosec B608 annotations silence the false-positive SQLi warning that
    # bandit emits when it sees string concatenation near SQL keywords.

    # ── api_mutation branch ───────────────────────────────────────────────
    if "api_mutation" in active_types:
        conds = ["1=1"]
        if since is not None:
            conds.append("ts > ?")
            params.append(since)
        _sel = (
            "SELECT ts, 'api_mutation' AS type, id AS source_id, "  # nosec B608 — hardcoded literals, no user input interpolated
            "NULL AS operation, NULL AS subject_key, NULL AS subject_type, "
            "NULL AS status, NULL AS triggered_by, NULL AS error, "
            "action, resource_id, response_status, actor, correlation_id, "
            "NULL AS check_name, NULL AS hch_summary, "
            "NULL AS provider, NULL AS model, NULL AS purpose, "
            "NULL AS cost_usd, NULL AS prompt_tokens, NULL AS completion_tokens, "
            "NULL AS sanitized "
            "FROM audit_log" + (" WHERE " + " AND ".join(conds) if conds else "")
        )
        branches.append(_sel)

    # ── operation branch ──────────────────────────────────────────────────
    if "operation" in active_types:
        conds = ["1=1"]
        if since is not None:
            conds.append("started_at > ?")
            params.append(since)
        _sel = (
            "SELECT started_at AS ts, 'operation' AS type, id AS source_id, "  # nosec B608 — hardcoded literals, no user input interpolated
            "operation, subject_key, subject_type, "
            "status, triggered_by, error, "
            "NULL AS action, NULL AS resource_id, NULL AS response_status, "
            "NULL AS actor, NULL AS correlation_id, "
            "NULL AS check_name, NULL AS hch_summary, "
            "NULL AS provider, NULL AS model, NULL AS purpose, "
            "NULL AS cost_usd, NULL AS prompt_tokens, NULL AS completion_tokens, "
            "NULL AS sanitized "
            "FROM operations" + (" WHERE " + " AND ".join(conds) if conds else "")
        )
        branches.append(_sel)

    # ── health_check branch ───────────────────────────────────────────────
    if "health_check" in active_types:
        conds = ["1=1"]
        if since is not None:
            conds.append("checked_at > ?")
            params.append(since)
        _sel = (
            "SELECT checked_at AS ts, 'health_check' AS type, id AS source_id, "  # nosec B608 — hardcoded literals, no user input interpolated
            "NULL AS operation, subject_key, subject_type, "
            "status, NULL AS triggered_by, NULL AS error, "
            "NULL AS action, NULL AS resource_id, NULL AS response_status, "
            "NULL AS actor, NULL AS correlation_id, "
            "check_name, summary AS hch_summary, "
            "NULL AS provider, NULL AS model, NULL AS purpose, "
            "NULL AS cost_usd, NULL AS prompt_tokens, NULL AS completion_tokens, "
            "NULL AS sanitized "
            "FROM health_check_history" + (" WHERE " + " AND ".join(conds) if conds else "")
        )
        branches.append(_sel)

    # ── llm_call branch ───────────────────────────────────────────────────
    if "llm_call" in active_types:
        conds = ["1=1"]
        if since is not None:
            conds.append("created_at > ?")
            params.append(since)
        _sel = (
            "SELECT created_at AS ts, 'llm_call' AS type, id AS source_id, "  # nosec B608 — hardcoded literals, no user input interpolated
            "NULL AS operation, NULL AS subject_key, NULL AS subject_type, "
            "NULL AS status, NULL AS triggered_by, NULL AS error, "
            "NULL AS action, NULL AS resource_id, NULL AS response_status, "
            "NULL AS actor, NULL AS correlation_id, "
            "NULL AS check_name, NULL AS hch_summary, "
            "provider, model, purpose, "
            "cost_usd, prompt_tokens, completion_tokens, sanitized "
            "FROM cloud_llm_usage" + (" WHERE " + " AND ".join(conds) if conds else "")
        )
        branches.append(_sel)

    if not branches:
        return {"events": [], "count": 0}

    # branches is a list of hardcoded string literals; no user input is
    # ever interpolated into the SQL text (user values go into `params`).
    sql = (
        "SELECT * FROM (\n"  # nosec B608  # noqa: S608 — branches are hardcoded string literals; no user input interpolated
        + "\nUNION ALL\n".join(branches)
        + "\n) ORDER BY ts DESC, source_id DESC LIMIT ?"
    )
    params.append(limit)

    with StateDB() as db:
        rows = db.execute(sql, tuple(params)).fetchall()

    events = []
    for r in rows:
        row = dict(r)
        events.append(
            {
                "ts": row["ts"],
                "type": row["type"],
                "source_id": row["source_id"],
                "summary": _build_summary(row),
                "detail": _build_detail(row),
            }
        )

    return {"events": events, "count": len(events)}
