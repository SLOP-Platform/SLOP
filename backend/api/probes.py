"""backend/api/probes.py — Kubernetes-style health probes (step 4.2).

Three endpoints, each answering a different operational question:

  - /healthz  — liveness: "is the process alive and accepting connections?"
                Always returns 200. Never queries dependencies.
                K8s restarts the pod if this 5xxs or times out.

  - /readyz   — readiness: "should the load balancer route traffic to me?"
                Returns 200 when DB is reachable + state.configure() set.
                Returns 503 with diagnostic body otherwise.

  - /startupz — startup: "have I finished my one-time initialization?"
                Returns 200 once migrations applied + scheduler armed.
                Returns 503 during the startup window so K8s holds off
                on liveness probes until startup completes.

These three endpoints sit OUTSIDE the `/api/v1/` versioning umbrella —
they're operational infrastructure, not the application API. They
follow their own change-management discipline (Core Rule 4.20).
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Response

from backend.core import state as _state_mod


router = APIRouter(tags=["System"], include_in_schema=False)


def _is_startup_complete() -> bool:
    """Read the startup-complete flag from state.py.

    The flag lives in `backend.core.state._STARTUP_COMPLETE` (not
    here) to avoid `state.py -> api/probes.py` imports — the
    dependency arrow must point `api/` -> `core/`, never the reverse.
    State sets the flag to True at the end of `init_db()`.
    """
    return _state_mod._STARTUP_COMPLETE


# ── /healthz ───────────────────────────────────────────────────────────────


@router.get("/healthz")
def healthz() -> dict[str, Any]:
    """Liveness probe — process is alive and serving HTTP.

    Per K8s convention this MUST NOT touch any dependency: a transient
    DB hiccup should not cause the pod to be killed. The probe answers
    a single question: did Python receive the request and reach this
    handler? If yes, return 200; the orchestrator can stop worrying
    about the process and start worrying about its dependencies (which
    is what /readyz is for).
    """
    return {"status": "ok", "ts": int(time.time())}


# ── /readyz ───────────────────────────────────────────────────────────────


@router.get("/readyz")
def readyz(response: Response) -> dict[str, Any]:
    """Readiness probe — should we receive traffic?

    Checks:
      - state.configure() has been called (database path is known)
      - StateDB ping (SELECT 1) returns within 1s

    Returns 503 with a diagnostic `checks` map if any check fails.
    K8s removes the pod from the load-balancer pool until /readyz
    returns 200 again.
    """
    checks: dict[str, str] = {}
    ok = True

    if _state_mod._DB_PATH is None:
        checks["state_configured"] = "fail: state.configure() not called"
        ok = False
    else:
        checks["state_configured"] = "ok"
        try:
            with _state_mod.StateDB() as db:
                db.execute("SELECT 1").fetchone()
            checks["db_ping"] = "ok"
        except Exception as e:
            checks["db_ping"] = f"fail: {type(e).__name__}: {e}"
            ok = False

    if not ok:
        response.status_code = 503
    return {"status": "ok" if ok else "not_ready", "checks": checks}


# ── /startupz ─────────────────────────────────────────────────────────────


@router.get("/startupz")
def startupz(response: Response) -> dict[str, Any]:
    """Startup probe — has the process completed its initialization phase?

    Returns 200 once migrations have been applied (the one-time work
    SLOP does on first boot). Returns 503 during the startup
    window. K8s uses this to delay /healthz checking — `failureThreshold
    x periodSeconds` covers the cold-start migration window.

    Once true, stays true for the life of the process. A process whose
    /startupz once returned 200 will never regress to 503 — that would
    be a different failure mode (process crash, /healthz handles it).
    """
    if _is_startup_complete():
        return {"status": "ok", "startup_complete": True}
    response.status_code = 503
    return {"status": "starting", "startup_complete": False}
