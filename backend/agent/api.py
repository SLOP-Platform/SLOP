"""backend/agent/api.py

Agent REST endpoints — Phase D/E.

GET  /api/v1/agent/diagnoses       — pending LLM-generated diagnoses
POST /api/v1/agent/fixes/{id}/apply — safe auto-apply tier (Phase E)

This router is registered in backend/api/main.py via _mount().
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from backend.core.logging import get_logger
from backend.core.state import StateDB

log = get_logger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class DiagnosisOut(BaseModel):
    id: int
    app_key: str
    problem: str
    diagnosis_class: str
    suggested_fix: str
    confidence: float
    status: str
    created_at: int


class DiagnosesResponse(BaseModel):
    diagnoses: list[DiagnosisOut]


# ---------------------------------------------------------------------------
# GET /diagnoses
# ---------------------------------------------------------------------------


@router.get("/diagnoses", response_model=DiagnosesResponse)
def get_diagnoses() -> DiagnosesResponse:
    """Return all pending diagnoses that have a non-empty suggested_fix.

    Ordered by created_at DESC, limited to 50 rows.  Only rows with
    status='pending' and suggested_fix != '' are included — empty
    suggested_fix means the LLM was unreachable and there is nothing
    actionable to show the user.
    """
    with StateDB() as db:
        rows = db.execute(
            """
            SELECT id, app_key, problem, diagnosis_class,
                   suggested_fix, confidence, status, created_at
            FROM   pending_fixes
            WHERE  status = 'pending'
              AND  suggested_fix != ''
            ORDER  BY created_at DESC
            LIMIT  50
            """,
        ).fetchall()

    diagnoses = [
        DiagnosisOut(
            id=row["id"],
            app_key=row["app_key"],
            problem=row["problem"],
            diagnosis_class=row["diagnosis_class"],
            suggested_fix=row["suggested_fix"],
            confidence=row["confidence"],
            status=row["status"],
            created_at=row["created_at"],
        )
        for row in rows
    ]
    return DiagnosesResponse(diagnoses=diagnoses)


# ---------------------------------------------------------------------------
# POST /fixes/{id}/apply — Phase E: safe auto-apply tier
# ---------------------------------------------------------------------------


@router.post("/fixes/{fix_id}/apply", status_code=200)
def apply_fix(fix_id: int) -> Any:
    """Apply a suggested fix (Phase E: safe auto-apply tier only).

    Safe fix types (handled automatically):
      restart_container  — docker restart <app_key>
      repull_restart     — docker pull + docker restart
      env_var_format     — compose fragment substitution (Phase H, future)

    Unsafe/unknown fix types → HTTP 422 (requires human review).
    Missing fix record      → HTTP 404.
    Docker command failure  → HTTP 500 with error detail.
    """
    from backend.agent.apply import SAFE_FIX_TYPES, apply_safe_fix, get_fix_type

    # 1. Fetch the fix record.
    with StateDB() as db:
        row = db.execute(
            """
            SELECT id, app_key, problem, diagnosis_class, suggested_fix,
                   fix_metadata, status
            FROM   pending_fixes
            WHERE  id = ?
            """,
            (fix_id,),
        ).fetchone()

    if row is None:
        return JSONResponse(status_code=404, content={"detail": f"Fix {fix_id} not found"})

    # 2. Derive fix_type and check it is in the safe tier.
    fix_type = get_fix_type(row["diagnosis_class"])
    if fix_type not in SAFE_FIX_TYPES:
        return JSONResponse(
            status_code=422,
            content={
                "detail": (
                    f"Fix type '{fix_type or row['diagnosis_class']}' requires human approval "
                    "(not in safe auto-apply tier)"
                )
            },
        )

    # 2.5 Governance gate (#977): route this REST apply through the SAME `authorize()`
    #     chokepoint the scheduler uses (scheduler.py `_authorize`). Before this, the REST
    #     path was the WORST bypass — an externally-triggerable HTTP endpoint that ran docker
    #     restart/pull gated only on backoff + SAFE_FIX_TYPES, skipping authorize()'s shared
    #     budget + ADVISORY/kill-switch level entirely (#977 / DToC consensus Q3 Option A:
    #     `.claude/run/l3-37-981-dtoc-consensus.md`). A human-initiated REST apply is
    #     `pre_approved=True` (the explicit apply IS the approval — mirrors the scheduler's
    #     policy authority), so the default SUPERVISED flow still allows; but ADVISORY, the
    #     kill-switch, and the shared per-app/global budget now bind. Fail-closed: any error
    #     consulting the gate denies execution (never an unmetered bypass).
    try:
        from backend.agent.governance import authorize
        from backend.agent.registry import tier_for
        from backend.agent.types import OperationalLevel

        with StateDB() as gdb:
            op_level = OperationalLevel.from_setting(gdb.get_setting("agent_operational_level"))
        gate = authorize(
            action_id=fix_type,
            app_key=row["app_key"],
            tier=tier_for(fix_type),
            operational_level=op_level,
            pre_approved=True,
        )
    except Exception as gov_err:  # fail-closed — do not execute if the gate can't be consulted
        log.warning("apply_fix: governance gate unavailable for fix_id=%s: %s", fix_id, gov_err)
        return JSONResponse(
            status_code=503,
            content={"detail": f"governance gate unavailable (fail-closed): {gov_err}"},
        )
    if not gate.allow:
        log.info(
            "apply_fix: governance gate denied fix_id=%s app=%s — %s",
            fix_id,
            row["app_key"],
            gate.reason,
        )
        return JSONResponse(status_code=403, content={"detail": f"governance gate: {gate.reason}"})

    # 3. Execute the fix.
    import subprocess

    try:
        result = apply_safe_fix(fix_id, row, trigger="api", operational_level=op_level)
    except subprocess.CalledProcessError as exc:
        log.warning("apply_fix: docker command failed for fix_id=%s: %s", fix_id, exc)
        return JSONResponse(
            status_code=500,
            content={"detail": f"Docker command failed: {exc}"},
        )
    except subprocess.TimeoutExpired as exc:
        log.warning("apply_fix: docker command timed out for fix_id=%s: %s", fix_id, exc)
        return JSONResponse(
            status_code=500,
            content={"detail": f"Docker command timed out: {exc}"},
        )

    # 4. Return result.
    return result
