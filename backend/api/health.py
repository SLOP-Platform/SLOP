"""backend/api/health.py

Health monitoring API routes.

GET  /api/health/status              — last health run summary
GET  /api/health/apps                — all app health check results
GET  /api/health/apps/{key}          — single app health
POST /api/health/run                 — trigger a health cycle immediately
GET  /api/health/llm-agent           — LLM agent status
POST /api/health/scheduler/pause — engage agent kill-switch
POST /api/health/scheduler/unpause   — release agent kill-switch
GET  /api/health/agent-audit         — recent agent action audit rows (#978)
"""

from __future__ import annotations

import time
from typing import Any

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from pydantic import BaseModel, Field

from backend.api import health_proposals
from backend.api.rate_limit import limiter
from backend.core.error_detail import safe_detail
from backend.core.logging import get_logger
from backend.core.state import StateDB, is_scheduler_paused, set_scheduler_paused
from backend.core.url_guard import UrlNotAllowed, assert_not_metadata_url
from backend.core.url_guard_httpx import pinned_async_client
from backend.health.checker import _llm_state, run_health_cycle
from backend.health.fix_verification import schedule_fix_verification

# Pending-actions computation extracted to health_pending.py (#1302 linecount
# drain). Re-export PendingAction (route response_model + tests). _env_cache moved
# there too (its canonical home now; the test-isolation registry resets it there).
from backend.api.health_pending import (
    PendingAction as PendingAction,
    compute_pending_actions,
)

log = get_logger(__name__)
router = APIRouter()

# LLM test-proposal endpoints live in a split-out sub-router (#1302 linecount
# drain). Included at module load — before _mount() in main.py runs — so the
# routes inherit the same prefix + control-plane guard as the rest of /health.
router.include_router(health_proposals.router)

# Strong references to fire-and-forget background tasks. Without this, asyncio
# only holds a weak reference and the task can be garbage-collected mid-run.
_background_tasks: set[Any] = set()


@router.get("/pending-actions")
def get_pending_actions() -> list[PendingAction]:
    """Return outstanding platform issues, ordered by priority (see health_pending)."""
    return compute_pending_actions()


class AppHealthOut(BaseModel):
    app_key: str
    check_name: str
    status: str
    summary: str
    last_checked: str | None
    auto_fix: str | None
    last_checked_age_seconds: float | None = None


class AgentHealthOut(BaseModel):
    check_name: str
    status: str
    summary: str
    detail: str | None
    last_checked: str | None


class LLMAgentStatus(BaseModel):
    status: str
    consecutive_failures: int
    consecutive_slow: int
    description: str
    last_error: str = ""
    last_error_type: str = ""
    ollama_url: str = ""
    model_tried: str = ""
    last_success_at: int = 0
    configured_provider: str = "ollama"


@router.get("/apps", response_model=list[AppHealthOut])
def get_all_app_health() -> list[AppHealthOut]:
    now = time.time()
    with StateDB() as db:
        rows = db.execute(
            "SELECT * FROM health_checks WHERE subject_type='app' ORDER BY checked_at DESC"
        ).fetchall()
    seen: set[str] = set()
    results = []
    for row in rows:
        key = f"{row['subject_key']}:{row['check_name']}"
        if key in seen:
            continue
        seen.add(key)
        checked = row["checked_at"]
        results.append(
            AppHealthOut(
                app_key=row["subject_key"],
                check_name=row["check_name"],
                status=row["status"],
                summary=row["summary"] or "",
                last_checked=datetime.fromtimestamp(checked).isoformat() if checked else None,
                auto_fix=row["auto_fix"] if "auto_fix" in row.keys() else None,
                last_checked_age_seconds=round(now - checked, 1) if checked else None,
            )
        )
    return results


@router.get("/apps/{key}", response_model=list[AppHealthOut])
def get_app_health(key: str) -> list[AppHealthOut]:
    now = time.time()
    with StateDB() as db:
        rows = db.execute(
            "SELECT * FROM health_checks WHERE subject_type='app' AND subject_key=? ORDER BY checked_at DESC",
            (key,),
        ).fetchall()
    return [
        AppHealthOut(
            app_key=r["subject_key"],
            check_name=r["check_name"],
            status=r["status"],
            summary=r["summary"] or "",
            last_checked=datetime.fromtimestamp(r["checked_at"]).isoformat()
            if r["checked_at"]
            else None,
            auto_fix=r["auto_fix"] if "auto_fix" in r.keys() else None,
            last_checked_age_seconds=round(now - r["checked_at"], 1) if r["checked_at"] else None,
        )
        for r in rows
    ]


@router.get("/agent")
def get_agent_health() -> dict[str, Any]:
    """Return SLOP Agent (tier-0) health check records plus swallow counters.

    Distinct from /apps health — uses subject_type='agent' rows so it never
    mixes with user-installed app checks.

    Response shape:
      {
        "checks": [ {check_name, status, summary, detail, last_checked}, ... ],
        "swallow_counters": {
          "total": <int>,
          "sites": { "<site>": {"count": int, "first_seen": ts, "last_seen": ts}, ... }
        }
      }
    """
    from backend.health.swallow_counter import get_swallow_counts

    with StateDB() as db:
        rows = db.execute(
            "SELECT * FROM health_checks WHERE subject_type='agent' ORDER BY checked_at DESC"
        ).fetchall()
    checks = [
        {
            "check_name": r["check_name"],
            "status": r["status"],
            "summary": r["summary"] or "",
            "detail": r["detail"] if "detail" in r.keys() else None,
            "last_checked": datetime.fromtimestamp(r["checked_at"]).isoformat()
            if r["checked_at"]
            else None,
        }
        for r in rows
    ]
    return {
        "checks": checks,
        "swallow_counters": get_swallow_counts(),
    }


@router.get("/agent/reality")
def get_agent_reality() -> dict[str, Any]:
    """RealityView — GROUND-truth facts the running instance observes about itself."""
    from backend.core.agent import get_reality_view

    return get_reality_view()


@router.get("/agent/registry")
@limiter.limit("10/minute")  # type: ignore[untyped-decorator]  # read-only registry surface (#982)
def get_agent_registry(
    request: Request,
) -> dict[str, Any]:
    """Return the Agent Action Registry as read-only views — the full action
    vocabulary (id, tier, reversibility, executability, scopeability, rate limit,
    diagnosis classes, description) the autonomous agent can dispatch.

    Introspection surface for #982: the registry (``backend/agent/registry.py``)
    was reachable only in-process (chat / MCP via ``list_actions``); this exposes
    the same PINNED ``ActionView`` projection over HTTP. Lists EVERY declared
    action — including declared-but-pending (non-executable) ones — so a client
    sees the whole surface and its tiers. Carries no handler reference: read-only,
    never mutates and never dispatches an action.

    Gated by the READ control-plane scope to match the sibling agent-domain review
    surfaces ``/agent-audit`` and ``/advisories``: open in the local no-token
    model, enforced once a control-plane token is configured (fail-closed under
    hardening)."""
    from dataclasses import asdict

    from backend.agent.registry import list_actions

    return {"actions": [asdict(v) for v in list_actions()]}


@router.get("/summary")
def get_health_summary() -> dict[str, Any]:
    """Return lightweight ok/warning/error counts — for sidebar display.
    Avoids fetching all check details just to count statuses.

    Response shape:
      ok / warning / error / unknown  — integer counts of app health checks
      agent_status                    — string status of the SLOP Agent itself
      process_integrity_status        — string status of SLOP's rule-enforcement
                                        coverage (executive-manager dimension)
      last_cycle_age_seconds          — seconds since the last completed health cycle
                                        (None if the cycle has never run); enables
                                        distinguishing "checked 30s ago, healthy" from
                                        "scheduler died 2h ago, stale green"
      scheduler_alive                 — True when the scheduler task is running and has
                                        not raised an exception; False means the task has
                                        exited (done/cancelled/errored) and health data
                                        is no longer being refreshed
    """
    from backend.core.agent import AGENT_INTEGRITY_KEY, AGENT_SUBJECT_TYPE_INTEGRITY
    from backend.core.state import StateDB
    from backend.health.scheduler import scheduler_status

    with StateDB() as db:
        app_rows = db.execute(
            "SELECT status, COUNT(*) as n FROM health_checks "
            "WHERE subject_type='app' GROUP BY status"
        ).fetchall()
        agent_row = db.execute(
            "SELECT status FROM health_checks "
            "WHERE subject_type='agent' AND subject_key='slop_agent' "
            "AND check_name='agent_status' LIMIT 1"
        ).fetchone()
        integrity_row = db.execute(
            "SELECT status FROM health_checks "
            "WHERE subject_type=? AND subject_key=? "
            "ORDER BY checked_at DESC LIMIT 1",
            (AGENT_SUBJECT_TYPE_INTEGRITY, AGENT_INTEGRITY_KEY),
        ).fetchone()
        last_cycle_raw = db.get_setting("health_last_cycle_at")

    counts: dict[str, Any] = {"ok": 0, "warning": 0, "error": 0, "unknown": 0}
    for r in app_rows:
        if r["status"] in counts:
            counts[r["status"]] = r["n"]
    counts["agent_status"] = agent_row["status"] if agent_row else "unknown"
    counts["process_integrity_status"] = integrity_row["status"] if integrity_row else "unknown"

    # Staleness signals — allow frontend to detect "stale green" (scheduler died)
    if last_cycle_raw:
        try:
            counts["last_cycle_age_seconds"] = round(time.time() - int(last_cycle_raw), 1)
        except (ValueError, TypeError):
            counts["last_cycle_age_seconds"] = None
    else:
        counts["last_cycle_age_seconds"] = None

    sched = scheduler_status()
    counts["scheduler_alive"] = sched.get("running", False) is True

    return counts


class IntegrityStatusOut(BaseModel):
    status: str
    critical_gaps: int
    high_gaps: int
    total_rules: int
    summary: str
    checked_at: int


@router.get("/integrity", response_model=IntegrityStatusOut)
def get_integrity_status() -> IntegrityStatusOut:
    """Return the latest process-integrity health dimension for the SLOP Agent."""
    from backend.core.agent import AGENT_INTEGRITY_KEY, AGENT_SUBJECT_TYPE_INTEGRITY
    import json as _json

    with StateDB() as db:
        row = db.execute(
            "SELECT status, summary, detail, checked_at FROM health_checks "
            "WHERE subject_type=? AND subject_key=? "
            "ORDER BY checked_at DESC LIMIT 1",
            (AGENT_SUBJECT_TYPE_INTEGRITY, AGENT_INTEGRITY_KEY),
        ).fetchone()
    if not row:
        return IntegrityStatusOut(
            status="unknown",
            critical_gaps=0,
            high_gaps=0,
            total_rules=0,
            summary="",
            checked_at=0,
        )
    counts: dict[str, int] = {}
    if row["detail"]:
        try:
            counts = _json.loads(row["detail"])
        except (ValueError, TypeError):
            pass
    return IntegrityStatusOut(
        status=row["status"],
        critical_gaps=counts.get("critical_gaps", 0),
        high_gaps=counts.get("high_gaps", 0),
        total_rules=counts.get("total_rules", 0),
        summary=row["summary"] or "",
        checked_at=row["checked_at"] or 0,
    )


@router.get("/llm-agent", response_model=LLMAgentStatus)
def get_llm_agent_status() -> LLMAgentStatus:
    status = _llm_state.get("status", "unknown")
    descriptions = {
        "active": "LLM responding quickly and producing valid JSON diagnoses.",
        "degraded": "LLM responses are slow or unreliable. Escalation-only mode.",
        "offline": "LLM unreachable. Rule-based healing only.",
        "disabled": "LLM agent explicitly disabled. Rule-based healing only.",
        "unknown": "LLM agent has not run yet this session.",
    }
    # Build a rich description when there's an error
    base_desc = descriptions.get(status, "Unknown state.")
    last_err = _llm_state.get("last_error", "")
    url = _llm_state.get("ollama_url", "") or "http://localhost:11434"
    model = _llm_state.get("model_tried", "") or "phi4-mini"
    err_type = _llm_state.get("last_error_type", "")

    if status == "offline" and last_err:
        _provider = _llm_state.get("configured_provider", "ollama") or "ollama"
        _pname = "Ollama" if _provider == "ollama" else _provider
        if err_type == "connection":
            base_desc = (
                f"Cannot reach {_pname} at {url}. Check that {_pname} is installed and running."
            )
        elif err_type == "model":
            if _provider == "ollama":
                base_desc = f"Model '{model}' not found in Ollama. Run: ollama pull {model}"
            else:
                base_desc = f"Model '{model}' not found in {_pname}."
        elif err_type == "dns":
            base_desc = f"DNS lookup failed for {url}. Check network connectivity and container name resolution."
        elif err_type == "auth":
            base_desc = f"Authentication to {_pname} failed. Verify API keys, tokens, and credentials."
        elif err_type == "timeout":
            base_desc = f"Model '{model}' took too long to respond. Try a smaller/faster model."
        elif err_type == "parse":
            base_desc = f"Model '{model}' returned malformed output. Try a different model."

    return LLMAgentStatus(
        status=status,
        consecutive_failures=_llm_state.get("consecutive_failures", 0),
        consecutive_slow=_llm_state.get("consecutive_slow", 0),
        description=base_desc,
        last_error=last_err,
        last_error_type=err_type,
        ollama_url=url,
        model_tried=model,
        last_success_at=_llm_state.get("last_success_at", 0),
        configured_provider=_llm_state.get("configured_provider", "ollama") or "ollama",
    )


def _run_health_cycle_bg() -> None:
    """Sync wrapper for run_health_cycle — intended for BackgroundTasks.

    Loads config from DB and runs the health cycle. The rich sync return
    (apps_checked, llm_state, etc.) is persisted to the health DB by the
    cycle itself; the health DB is the source of truth for those values.
    """
    import asyncio
    import json

    try:
        with StateDB() as db:
            cfg = db.get_setting("llm_agent_config")
        agent_cfg = json.loads(cfg) if cfg else {}
        _provider = agent_cfg.get("provider", "ollama")
        if _provider == "llamacpp":
            ollama_url = agent_cfg.get("llamacpp_url", "http://localhost:8081")
        else:
            ollama_url = agent_cfg.get("ollama_url", "http://ollama:11434")
        ntfy_topic = agent_cfg.get("ntfy_topic", "slop")
        asyncio.run(
            run_health_cycle(
                ollama_url=ollama_url,
                ntfy_url="http://ntfy:80",
                ntfy_topic=ntfy_topic,
            )
        )
    except Exception as _e:
        log.warning("Background health cycle failed: %s: %s", type(_e).__name__, _e)


@router.post("/run")
@limiter.limit("10/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped (Step 2.4 — heavy read tier)
async def trigger_health_run(request: Request, background_tasks: BackgroundTasks) -> dict[str, Any]:
    """Trigger a health cycle immediately (runs in background; health DB is the source of truth)."""
    try:
        background_tasks.add_task(_run_health_cycle_bg)
    except Exception as _e:
        raise HTTPException(
            status_code=500, detail=f"Failed to schedule health cycle: {type(_e).__name__}: {_e}"
        ) from _e
    return {"ok": True, "started": True, "message": "Health cycle started in background."}


@router.get("/scheduler", tags=["Health"])
def get_scheduler_status() -> dict[str, Any]:
    """Return the health scheduler status.

    Shows whether the background check scheduler is running,
    the last cycle time, and last cycle results.
    """
    from backend.health.scheduler import scheduler_status

    status = scheduler_status()

    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            last_at = db.get_setting("health_last_cycle_at")
            last_summary = db.get_setting("health_last_cycle_summary")
        import json
        import datetime as _dt

        summary = json.loads(last_summary) if last_summary else None
        last_ts = _dt.datetime.fromtimestamp(int(last_at)).isoformat() if last_at else None
    except Exception:
        summary = None
        last_ts = None

    return {
        **status,
        "last_cycle_at": last_ts,
        "last_cycle_summary": summary,
        "kill_switch_engaged": is_scheduler_paused(),
    }


@router.get("/probe-failures")
@limiter.limit("10/minute")  # type: ignore[untyped-decorator]  # read-only probe failure surface (id=G1)
def get_probe_failures(request: Request) -> dict[str, Any]:
    """Return all probes that have hit 5+ consecutive failures, ordered by most recent."""
    with StateDB() as db:
        return {"probe_failures": db.get_probe_failures()}


@router.get("/advisories")
@limiter.limit("10/minute")  # type: ignore[untyped-decorator]  # read-only advisory surface (#1089)
def list_spine_advisories(
    request: Request,
    limit: int = 100,
) -> dict[str, Any]:
    """Return the store-only spine advisories — advisory LLM annotations persisted
    alongside the GROUND verdicts for human review (the read surface for #1089;
    the table was previously write-only). Newest first. Read-only: never mutates
    and never triggers remediation. ``limit`` is clamped to 1..500.

    Gated by the READ control-plane scope to match the sibling agent-domain review
    surface ``/agent-audit``: open in the local no-token model, enforced once a
    control-plane token is configured (fail-closed under hardening)."""
    with StateDB() as db:
        return {"advisories": db.get_spine_advisories(limit=min(max(limit, 1), 500))}


@router.post("/scheduler/pause", tags=["Health"])
def pause_scheduler() -> dict[str, Any]:
    """Engage the agent kill-switch — pause all autonomous scheduler mutations.

    The health-check loop continues to run and report findings; only
    autonomous fix actions are suppressed until ``POST /scheduler/unpause``.
    The flag resets to False on process restart (intentional — a paused
    scheduler that survives a restart without operator awareness is a footgun).

    Requires the ACT control-plane scope (``SLOP_CONTROL_PLANE_TOKEN`` or
    ``control_plane_token`` setting).  When no token is configured the
    endpoint returns 403 (fail-closed — never silently allow a kill-switch
    without an explicit credential).
    """
    try:
        set_scheduler_paused(True)
    except Exception as e:  # pragma: no cover - flag setter is in-memory, but never 500 bare
        raise HTTPException(
            status_code=500, detail=safe_detail(e, "Failed to engage kill-switch.", log=log)
        ) from e
    log.info("scheduler kill-switch ENGAGED via API")
    return {
        "ok": True,
        "paused": True,
        "message": (
            "Agent kill-switch engaged. Autonomous mutations are suppressed; "
            "health checks continue. POST /scheduler/unpause to resume."
        ),
    }


@router.post("/scheduler/unpause", tags=["Health"])
def unpause_scheduler() -> dict[str, Any]:
    """Release the agent kill-switch — allow autonomous scheduler mutations again.

    Requires the ACT control-plane scope.  No-op if the scheduler is not
    currently paused.
    """
    try:
        was_paused = is_scheduler_paused()
        set_scheduler_paused(False)
    except Exception as e:  # pragma: no cover - flag setter is in-memory, but never 500 bare
        raise HTTPException(
            status_code=500, detail=safe_detail(e, "Failed to release kill-switch.", log=log)
        ) from e
    log.info("scheduler kill-switch RELEASED via API (was_paused=%s)", was_paused)
    return {
        "ok": True,
        "paused": False,
        "was_paused": was_paused,
        "message": "Agent kill-switch released. Autonomous mutations may resume.",
    }


@router.get("/agent-audit", tags=["Health"])
def get_agent_audit(
    limit: int = 20,
    app_key: str | None = None,
) -> dict[str, Any]:
    """Return recent agent action audit rows (#978 / N7).

    The audit trail is the authoritative "what did you do" source — every
    autonomous fix (pre-approved or chat-dispatched) writes a QUEUED row
    before execution and an OUTCOME row (ok/failed/rolled_back) after.

    Optional query parameters:
      limit    — max rows to return (default 20, max 100)
      app_key  — filter by app (returns only outcome rows for that app)
    """
    from backend.agent.audit import get_recent_actions

    rows = get_recent_actions(limit=min(max(1, limit), 100), app_key=app_key)
    return {
        "ok": True,
        "count": len(rows),
        "rows": rows,
        "scheduler_paused": is_scheduler_paused(),
    }


@router.put("/settings", tags=["Health"])
def update_health_settings(
    interval_secs: int | None = None,
    ntfy_topic: str | None = None,
    ollama_url: str | None = None,
) -> dict[str, Any]:
    """Update health scheduler settings.

    interval_secs: seconds between check cycles (minimum 10, default 30)
    ntfy_topic: ntfy topic for failure notifications
    ollama_url: Ollama base URL for LLM agent

    Changes take effect at the next cycle — no restart needed.
    """
    from backend.core.state import StateDB

    updated: dict[str, Any] = {}
    try:
        with StateDB() as db:
            if interval_secs is not None:
                val = max(30, interval_secs)  # 30s minimum matches DNS challenge delay
                db.set_setting("health_check_interval_secs", str(val))
                updated["health_check_interval_secs"] = val
            if ntfy_topic is not None:
                db.set_setting("ntfy_topic", ntfy_topic)
                updated["ntfy_topic"] = ntfy_topic
            if ollama_url is not None:
                import json as _json

                existing = db.get_setting("llm_agent_config") or "{}"
                cfg = _json.loads(existing)
                cfg["ollama_url"] = ollama_url
                db.set_setting("llm_agent_config", _json.dumps(cfg))
                updated["ollama_url"] = ollama_url
    except Exception as _e:
        raise HTTPException(
            status_code=500, detail=f"Failed to update health settings: {type(_e).__name__}: {_e}"
        ) from _e
    return {"ok": True, "updated": updated}


# ── Pending actions ────────────────────────────────────────────────────────


# ── Anomaly detection ──────────────────────────────────────────────────────


@router.get("/anomalies")
def get_anomalies() -> list[dict[str, Any]]:
    """Return recurring failure patterns detected across health check history."""
    from backend.health.anomaly import get_anomaly_summary

    return get_anomaly_summary()


# ── Platform health review (LLM reviews pending actions) ───────────────────


@router.post("/platform-review")
async def run_platform_review() -> dict[str, Any]:
    """Ask the LLM to review pending actions and suggest fixes.

    The LLM gets:
    - Full pending actions list with priorities
    - Current platform state
    - RAG knowledge base context

    Returns plain-language summary and per-action suggestions.
    """
    from backend.core.state import StateDB
    from backend.core.rag import enrich_prompt_with_context

    # Get pending actions
    try:
        actions = compute_pending_actions()
    except Exception as e:
        return {"ok": False, "error": safe_detail(e, "Could not get pending actions.", log=log)}

    if not actions:
        return {
            "ok": True,
            "summary": "No pending actions — platform looks healthy.",
            "suggestions": [],
        }

    with StateDB() as db:
        platform = db.get_platform()

    # Build prompt
    actions_text = "\n".join(
        f"[{a.priority.upper()}] {a.title}: {a.description} → {a.action}" for a in actions
    )
    prompt = f"""You are a homelab infrastructure assistant reviewing a SLOP server.

Platform: domain={platform.domain}, status={platform.status}

Current issues requiring attention:
{actions_text}

For each issue:
1. Confirm if the suggested action is correct
2. Add any additional context or caveats
3. Flag if any issues are related to each other
4. Prioritize which to fix first

Respond as a helpful assistant in plain language. Be concise — 2-3 sentences per issue max."""

    prompt = enrich_prompt_with_context(prompt, actions_text)

    # removed: status "ready" never emitted by LLM state machine; block was dead code
    # No local LLM — return structured analysis without AI
    error_count = sum(1 for a in actions if a.priority == "error")
    warning_count = sum(1 for a in actions if a.priority == "warning")
    return {
        "ok": True,
        "summary": (
            f"Found {error_count} error(s) and {warning_count} warning(s). "
            "Install Ollama and a model to get AI-powered analysis."
        ),
        "provider": None,
        "action_count": len(actions),
        "suggestions": [
            {"title": a.title, "action": a.action, "priority": a.priority} for a in actions
        ],
    }


# ── Apply AI suggestion ────────────────────────────────────────────────────


class ApplyRequest(BaseModel):
    app_key: str
    action_type: str
    suggested_fix: str
    # Diagnosed problem text (#1160). Optional for backward compatibility; when
    # supplied it lets /apply-fix route the outcome through the record_fix_outcome
    # seam with a real signature_hash (the error-text-keyed recipe the learning
    # read path recomputes), instead of the signature-less pending bypass.
    problem: str = ""


@router.post("/apply-fix")
async def apply_suggested_fix(req: ApplyRequest) -> dict[str, Any]:
    """Execute an AI-suggested fix, respecting the safety tier.

    The safety tier must be set to 'act' for auto-execution, or the
    user must have explicitly confirmed via the UI (handled by frontend
    showing a confirmation modal before calling this endpoint).
    """
    from backend.core.ai_safety import execute_action, get_safety_level

    level = get_safety_level(req.action_type)
    if level == "observe":
        return {
            "ok": False,
            "requires_approval": False,
            "message": f"Safety level for '{req.action_type}' is 'observe' — no actions allowed.",
        }

    result = await execute_action(
        req.action_type,
        req.app_key,
        req.suggested_fix,
        approved=True,  # user explicitly triggered via /apply-fix endpoint
        caller_context="health_api_apply_fix",
    )

    # Record in fix_history. With a diagnosed PROBLEM (#1160) the write routes
    # through the single agent↔API seam (#822, record_fix_outcome) so it carries
    # the error-text-keyed signature_hash/diagnosis_class the learning store keys
    # on (a cache hit on the next identical failure). Without one, keep the
    # deliberate bypass: a signature-less pending row (never counted) over noise.
    try:
        from backend.core.state import StateDB
        import time as _time

        if req.problem:
            # Outcome mirrors approve_fix's derivation from the action result.
            if result.get("executed"):
                outcome = "success"
            elif result.get("requires_approval"):
                outcome = "user_approved_manual"
            else:
                outcome = "pending"
            from backend.agent.fix_outcome import record_fix_outcome

            with StateDB() as db:
                record_fix_outcome(
                    db,
                    app_key=req.app_key,
                    problem=req.problem,
                    error_type=req.action_type,
                    context=req.problem,
                    suggested_fix=req.suggested_fix,
                    outcome=outcome,
                )
        else:
            with StateDB() as db:
                db.execute(
                    """INSERT INTO fix_history
                       (app_key, error_type, context, suggested_fix, outcome, created_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        req.app_key,
                        req.action_type,
                        req.suggested_fix[:200],
                        req.suggested_fix,
                        "pending",
                        int(_time.time()),
                    ),
                )
    except Exception as e:
        log.debug("health API best-effort step skipped: %s", e)

    return result


# ── Weekly health summary ──────────────────────────────────────────────────


# ── Ghost resource management ───────────────────────────────────────────────


@router.get("/ghost-resources")
def get_ghost_resources() -> dict[str, Any]:
    """Return ghost containers, fragments, and volumes.

    A ghost is a Docker resource that exists but is not tracked in SLOP DB,
    or an app in DB that has no corresponding running container.
    """
    import subprocess
    from backend.core.state import StateDB
    from backend.core.config import config as _cfg

    ghost_containers: list[dict[str, Any]] = []
    ghost_fragments: list[dict[str, Any]] = []
    orphaned_apps: list[dict[str, Any]] = []

    _INFRA = {
        "traefik",
        "cloudflared",
        "tinyauth",
        "gluetun",
        "portainer",
        "authelia",
        "headscale",
        "tailscale",
        "glance",
        "homepage",
        "dockge",
        "dockhand",
        "komodo",
        "portainer_be",
    }

    # Get running containers from Docker
    running: dict[str, str] = {}
    try:
        r = subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if r.returncode == 0:
            for line in r.stdout.strip().splitlines():
                parts = line.split("\t")
                if len(parts) >= 3:
                    running[parts[0]] = f"{parts[1]} ({parts[2]})"
    except Exception as e:
        log.debug("Docker not available for container listing: %s", e)

    # DB apps
    with StateDB() as db:
        db_apps = {a.key: a for a in db.get_all_apps()}

    # Ghost containers: running but not in DB and not infra
    for name, info in running.items():
        if name not in _INFRA and name not in db_apps:
            ghost_containers.append({"name": name, "info": info})

    # Orphaned apps: in DB as running but container not found
    for key, app in db_apps.items():
        if getattr(app, "status", "") == "running":
            cname = getattr(app, "container_name", key) or key
            if cname not in running and key not in running:
                orphaned_apps.append(
                    {
                        "key": key,
                        "display_name": app.display_name or key,
                        "container_name": cname,
                    }
                )

    # Ghost fragments: compose files with no DB entry
    compose_dir = _cfg.compose_dir
    if compose_dir.exists():
        infra_files = {
            "traefik",
            "cloudflared",
            "tinyauth",
            "gluetun",
            "portainer",
            "authelia",
            "headscale",
            "tailscale",
        }
        for frag in sorted(compose_dir.glob("*.yaml")):
            k = frag.stem
            if k not in infra_files and k not in db_apps:
                ghost_fragments.append(
                    {
                        "filename": frag.name,
                        "key": k,
                        "size_bytes": frag.stat().st_size,
                    }
                )

    return {
        "ghost_containers": ghost_containers,
        "ghost_fragments": ghost_fragments,
        "orphaned_apps": orphaned_apps,
        "docker_available": bool(running) or True,
    }


class GhostAction(BaseModel):
    resource_type: str  # container | fragment | orphaned_app
    name: str
    action: str  # adopt | remove | ignore


@router.post("/ghost-resources/action")
def handle_ghost_resource(req: GhostAction) -> dict[str, Any]:
    """Act on a ghost resource: adopt, remove, or ignore."""
    import subprocess
    from backend.core.state import StateDB
    from backend.core.config import config as _cfg

    if req.action == "remove":
        if req.resource_type == "container":
            try:
                subprocess.run(
                    ["docker", "stop", req.name],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                subprocess.run(["docker", "rm", req.name], capture_output=True, timeout=15)
                return {"ok": True, "message": f"Container '{req.name}' stopped and removed."}
            except Exception as e:
                raise HTTPException(
                    502, detail=safe_detail(e, "Could not stop or remove the container.", log=log)
                ) from e

        elif req.resource_type == "fragment":
            frag = _cfg.compose_dir / req.name
            if frag.exists():
                frag.unlink()
                return {"ok": True, "message": f"Fragment '{req.name}' deleted."}
            raise HTTPException(404, f"Fragment '{req.name}' not found.")

        elif req.resource_type == "orphaned_app":
            with StateDB() as db:
                db.upsert_app(req.name, status="error")
            return {"ok": True, "message": f"'{req.name}' marked as error — reinstall or remove."}

    elif req.action == "adopt":
        if req.resource_type == "container":
            # Register the container in the DB
            with StateDB() as db:
                db.upsert_app(
                    req.name,
                    display_name=req.name.replace("_", " ").replace("-", " ").title(),
                    category="tools",
                    image="unknown",
                    container_name=req.name,
                    status="running",
                    config_path="",
                )
            return {"ok": True, "message": f"Container '{req.name}' adopted into SLOP."}

    elif req.action == "ignore":
        return {"ok": True, "message": f"'{req.name}' will be suppressed from ghost reports."}

    raise HTTPException(422, f"Unknown action: {req.action}")


# ── Weekly health history LLM summary ─────────────────────────────────────


@router.get("/weekly-summary")
async def get_weekly_summary() -> dict[str, Any]:
    """Generate a plain-language LLM summary of the last 7 days of health data.

    Returns: {summary, period, error_count, warning_count, top_issues, generated_at}
    """
    import time as _time
    from backend.core.state import StateDB
    from backend.core.rag import enrich_prompt_with_context

    cutoff = int(_time.time()) - 7 * 86400
    with StateDB() as db:
        # Get health history for the week
        try:
            rows = db.execute(
                """SELECT subject_key, check_name, status, summary, checked_at
                   FROM health_check_history
                   WHERE checked_at >= ? ORDER BY checked_at DESC LIMIT 200""",
                (cutoff,),
            ).fetchall()
        except Exception:
            rows = []

    error_count = sum(1 for r in rows if r["status"] == "error")
    warning_count = sum(1 for r in rows if r["status"] == "warning")

    # Find top issues (most frequent error/warning pairs)
    from collections import Counter

    issue_counter = Counter(
        f"{r['subject_key']}:{r['check_name']}" for r in rows if r["status"] in ("error", "warning")
    )
    top_issues = [
        {"app": k.split(":")[0], "check": k.split(":")[1], "count": v}
        for k, v in issue_counter.most_common(5)
    ]

    # Build LLM prompt
    issues_text = (
        "\n".join(f"- {i['app']} / {i['check']}: {i['count']} occurrence(s)" for i in top_issues)
        or "No issues recorded this week."
    )

    prompt = f"""You are a homelab assistant reviewing a week of health monitoring data.

Period: last 7 days
Total checks recorded: {len(rows)}
Errors: {error_count}
Warnings: {warning_count}

Top recurring issues:
{issues_text}

Write a brief (3-5 sentence) plain-language weekly summary for the homelab owner.
Include: overall health assessment, what needs attention, and any positive highlights.
Keep it conversational and helpful."""

    prompt = enrich_prompt_with_context(prompt, issues_text)

    # removed: status "ready" never emitted by LLM state machine; block was dead code
    summary = None

    if not summary:
        # Fallback: structured summary without LLM
        health = (
            "healthy"
            if error_count == 0
            else "needs attention"
            if error_count < 5
            else "has significant issues"
        )
        summary = (
            f"Your homelab {health} this week with {error_count} error(s) and "
            f"{warning_count} warning(s) recorded across {len(rows)} health checks. "
            + (
                f"Most frequent issue: {top_issues[0]['app']} ({top_issues[0]['count']}x)."
                if top_issues
                else "No recurring issues detected."
            )
        )

    return {
        "summary": summary,
        "period": "last 7 days",
        "error_count": error_count,
        "warning_count": warning_count,
        "top_issues": top_issues,
        "generated_at": int(_time.time()),
        "llm_used": _llm_state.get("status") in ("active", "degraded"),
    }


# ── Test result reporting for LLM context ────────────────────────────────


class TestResultFailure(BaseModel):
    """A single test failure record within TestResultPayload."""

    name: str = Field("", description="Test name or identifier")
    message: str = Field("", description="Failure message or traceback excerpt")


class TestResultPayload(BaseModel):
    """Typed request body for POST /test-results (id=466)."""

    passed: int = Field(0, ge=0, description="Number of passing tests")
    failed: int = Field(0, ge=0, description="Number of failing tests")
    errors: int = Field(0, ge=0, description="Number of errored tests")
    duration_s: float = Field(0.0, ge=0, description="Total run duration in seconds")
    failures: list[TestResultFailure] = Field(
        default_factory=list,
        description="Details of individual failures (capped at 20 stored)",
    )


@router.post("/test-results")
def record_test_results(payload: TestResultPayload) -> dict[str, Any]:
    """Ingest pytest run results so the LLM health agent can include them
    in weekly summaries and anomaly detection.

    Called by: pytest conftest.py post-run hook, or manually via ms-check.
    """
    from backend.core.state import StateDB
    import json
    import time as _time

    try:
        with StateDB() as db:
            db.set_setting("last_test_run_ts", str(int(_time.time())))
            db.set_setting("last_test_run_passed", str(payload.passed))
            db.set_setting("last_test_run_failed", str(payload.failed))
            db.set_setting("last_test_run_duration", str(payload.duration_s))
            failures = [f.model_dump() for f in payload.failures]
            db.set_setting("last_test_run_failures", json.dumps(failures[:20]))
    except Exception as _e:
        raise HTTPException(
            status_code=500, detail=f"Failed to record test results: {type(_e).__name__}: {_e}"
        ) from _e

    return {"ok": True, "recorded": True}


@router.get("/test-results")
def get_test_results() -> dict[str, Any]:
    """Return the most recent pytest run results."""
    from backend.core.state import StateDB
    import json
    import time as _time

    with StateDB() as db:
        ts = int(db.get_setting("last_test_run_ts") or "0")
        passed = int(db.get_setting("last_test_run_passed") or "0")
        failed = int(db.get_setting("last_test_run_failed") or "0")
        duration = float(db.get_setting("last_test_run_duration") or "0")
        failures_raw = db.get_setting("last_test_run_failures") or "[]"

    failures = []
    try:
        failures = json.loads(failures_raw)
    except Exception as e:
        log.debug("health API best-effort step skipped: %s", e)

    age_hours = (_time.time() - ts) / 3600 if ts else None
    return {
        "last_run_ts": ts or None,
        "age_hours": round(age_hours, 1) if age_hours else None,
        "passed": passed,
        "failed": failed,
        "duration_s": duration,
        "failures": failures,
        "status": "pass" if failed == 0 and passed > 0 else ("fail" if failed > 0 else "unknown"),
    }


# ── LLM test proposal system ──────────────────────────────────────────────
# Endpoints (propose-tests / proposed-tests) live in health_proposals.py and
# are mounted onto this router below via include_router (#1302 linecount drain),
# so their paths + control-plane guard are unchanged.


# ── Anomaly suppression / snooze ──────────────────────────────────────────


class AnomalySnoozeRequest(BaseModel):
    app_key: str
    check_name: str
    reason: str = ""
    hours: int = Field(72, ge=1, le=720)


@router.post("/anomalies/{app_key}/{check_name}/snooze")
def snooze_anomaly(app_key: str, check_name: str, req: AnomalySnoozeRequest) -> dict[str, Any]:
    """Snooze recurring anomaly alerts for an app/check pair.

    Used when you know why an app fails at a specific time (e.g. a backup
    job restarts a container at 03:00) and don't want it polluting anomaly
    reports. The anomaly is still recorded — just not shown as 'recurring'.
    """
    import time as _time
    from backend.core.state import StateDB

    snooze_until = int(_time.time()) + req.hours * 3600
    try:
        with StateDB() as db:
            db.set_setting(f"snooze_{app_key}_{check_name}", str(snooze_until))
    except Exception as _e:
        raise HTTPException(
            status_code=500, detail=f"Failed to snooze anomaly: {type(_e).__name__}: {_e}"
        ) from _e
    return {
        "ok": True,
        "snoozed_until": snooze_until,
        "hours": req.hours,
        "message": (
            f"Anomaly '{check_name}' for '{app_key}' snoozed for {req.hours}h. "
            f"Still recorded in history — just hidden from recurring issues panel."
        ),
    }


@router.get("/agent-config")
def get_agent_config() -> dict[str, Any]:
    """Return current LLM inference provider configuration."""
    from backend.core.state import StateDB
    import json as _json

    with StateDB() as db:
        raw = db.get_setting("llm_agent_config")
    cfg = _json.loads(raw) if raw else {}
    return {
        "provider": cfg.get("provider", "ollama"),
        "ollama_url": cfg.get("ollama_url", "http://localhost:11434"),
        "model": cfg.get("ollama_model", ""),
        "api_key": cfg.get("api_key", ""),
    }


@router.put("/agent-config")
def put_agent_config(
    provider: str | None = None,
    ollama_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
) -> dict[str, Any]:
    """Persist LLM inference provider config. Only supplied fields are updated."""
    from backend.core.state import StateDB
    import json as _json

    try:
        with StateDB() as db:
            raw = db.get_setting("llm_agent_config")
            cfg = _json.loads(raw) if raw else {}
            if provider is not None:
                cfg["provider"] = provider
            if ollama_url is not None:
                cfg["ollama_url"] = ollama_url
            if model is not None:
                cfg["ollama_model"] = model
            if api_key is not None:
                cfg["api_key"] = api_key
            db.set_setting("llm_agent_config", _json.dumps(cfg))
    except Exception as _e:
        raise HTTPException(
            status_code=500, detail=f"Failed to update agent config: {type(_e).__name__}: {_e}"
        ) from _e
    return {"ok": True, "config": cfg}


@router.get("/llm-ping")
async def ping_llm() -> dict[str, Any]:
    """Probe Ollama (or configured LLM backend) right now and return structured result.

    Used by the UI to get an immediate, accurate status without waiting for
    a health cycle to fail enough times to flip the state machine.
    """
    import httpx
    from backend.core.state import StateDB
    import json as _json

    with StateDB() as db:
        cfg_raw = db.get_setting("llm_agent_config")
    cfg = _json.loads(cfg_raw) if cfg_raw else {}
    provider = cfg.get("provider", "ollama")
    if provider == "llamacpp":
        base_url = cfg.get("llamacpp_url", "http://localhost:8081")
    else:
        base_url = cfg.get("ollama_url", "http://localhost:11434")
    api_key = cfg.get("api_key", "")

    try:
        from backend.core.llm_router import best_model_for

        rec = best_model_for("reasoning")
        model = (rec.ollama_name or rec.filename.replace(".gguf", "")) if rec else None
    except Exception:
        model = None
    if not model:
        model = cfg.get("ollama_model", "")

    INSTALL = {
        "ollama": "curl -fsSL https://ollama.com/install.sh | sh && ollama pull [model-name]",
        "llamacpp": "# Build llama-server: cmake llama.cpp -B build -DGGML_CUDA=ON && cmake --build build -t llama-server\n./build/bin/llama-server -m /path/to/model.gguf --port 8081",
        "shimmy": "curl -L https://github.com/Michael-A-Kuykendall/shimmy/releases/latest/download/shimmy-linux-x86_64 -o shimmy\nchmod +x shimmy && ./shimmy serve --bind 0.0.0.0:11435",
        "localai": "docker run -p 8080:8080 localai/localai:latest-aio-cpu",
        "groq": "# Sign up at console.groq.com → API Keys (free, no credit card)",
        "cerebras": "# Sign up at cloud.cerebras.ai → API Keys (free, 1M tokens/day)",
        "nim": "# Sign up at build.nvidia.com → Get API key (free, nvapi- prefix)",
        "gai": "# Sign up at aistudio.google.com → Get API key (free, generous limits)",
        "openrouter": "# Sign up at openrouter.ai → API Keys → Create key",
    }

    def _ok(models: list[Any]) -> dict[str, Any]:
        loaded = any(model in m for m in models) if model else bool(models)
        fix = ""
        if not loaded and model and provider == "ollama":
            fix = f"ollama pull {model}"
        return {
            "reachable": True,
            "model_loaded": loaded,
            "model": model or (models[0] if models else ""),
            "ollama_url": base_url,
            "loaded_models": models,
            "provider": provider,
            "error_type": "model" if not loaded else "",
            "error": f"No model loaded. {fix}" if not loaded else "",
            "fix": fix,
        }

    def _err(etype: str, msg: str, fix: str = "") -> dict[str, Any]:
        return {
            "reachable": False,
            "model_loaded": False,
            "model": model or "",
            "ollama_url": base_url,
            "loaded_models": [],
            "provider": provider,
            "error_type": etype,
            "error": msg,
            "fix": fix or INSTALL.get(provider, ""),
        }

    try:
        async with pinned_async_client(timeout=5) as client:
            if provider == "ollama":
                r = await client.get(f"{base_url}/api/tags")
                if r.status_code != 200:
                    return _err("http", f"Ollama returned HTTP {r.status_code}")
                return _ok([m.get("name", "") for m in r.json().get("models", [])])

            # ── Cloud providers — all use /v1/models or equivalent ──────
            elif provider in ("openrouter", "groq", "cerebras", "nim", "gai"):
                if not api_key:
                    return _err(
                        "auth",
                        f"API key required for {provider}.",
                        INSTALL.get(provider, f"# Sign up and get an API key for {provider}"),
                    )
                # Cerebras uses /v1/models, Google AI uses models list endpoint
                if provider == "gai":
                    list_url = "https://generativelanguage.googleapis.com/v1beta/openai/models"
                else:
                    list_url = f"{base_url}/models"
                r = await client.get(list_url, headers={"Authorization": f"Bearer {api_key}"})
                if r.status_code == 200:
                    data = r.json()
                    models_list = [m.get("id", "") for m in data.get("data", [])][:6]
                    return {
                        "reachable": True,
                        "model_loaded": True,
                        "model": model or "auto",
                        "ollama_url": base_url,
                        "loaded_models": models_list,
                        "provider": provider,
                        "error_type": "",
                        "error": "",
                        "fix": "",
                    }
                return _err(
                    "auth" if r.status_code == 401 else "http",
                    f"{provider} returned HTTP {r.status_code}",
                )

            else:  # llamacpp | shimmy | localai — all expose /v1/models
                headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
                r = await client.get(f"{base_url}/v1/models", headers=headers)
                if r.status_code == 200:
                    data = r.json()
                    ids = [
                        m.get("id", "")
                        for m in (data.get("data", data) if isinstance(data, dict) else data)
                    ]
                    return _ok(ids)
                if provider == "shimmy":  # shimmy also has /health
                    r2 = await client.get(f"{base_url}/health")
                    if r2.status_code == 200:
                        return _ok([])
                return _err("http", f"{provider} returned HTTP {r.status_code}")

    except httpx.ConnectError:
        return _err("connection", f"Cannot connect to {provider} at {base_url}.")
    except httpx.TimeoutException:
        return _err("timeout", f"Connection to {base_url} timed out.")
    except Exception as e:
        return _err("unknown", str(e)[:200])


# ── Maintenance windows ────────────────────────────────────────────────────


@router.get("/maintenance-windows")
def get_maintenance_windows() -> list[dict[str, Any]]:
    """Return all configured maintenance windows."""
    from backend.core.state import StateDB

    with StateDB() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS maintenance_windows (
                id INTEGER PRIMARY KEY, app_key TEXT NOT NULL,
                check_name TEXT NOT NULL, label TEXT NOT NULL DEFAULT 'Scheduled task',
                day_of_week INTEGER, hour_start INTEGER NOT NULL,
                hour_end INTEGER NOT NULL DEFAULT -1, enabled INTEGER NOT NULL DEFAULT 1,
                created_at INTEGER NOT NULL DEFAULT (unixepoch())
            )""")
        rows = db.execute("SELECT * FROM maintenance_windows ORDER BY created_at DESC").fetchall()
    return [dict(r) for r in rows]


class MaintenanceWindowIn(BaseModel):
    app_key: str
    check_name: str
    label: str = "Scheduled task"
    # Bounds reject out-of-domain ints at validation time (HTTP 422) instead of
    # letting them reach the DB — an oversized hour (e.g. 2**63) overflows
    # SQLite's signed-64-bit INTEGER → OverflowError → HTTP 500, and an
    # out-of-range day_of_week would IndexError day_names[...] downstream. Both
    # are adversarial-input 5xx (Core Rule: zero 500s on vetted-safe mutating
    # endpoints). 0=Mon … 6=Sun; None=every day. hour_end -1 = hour_start + 2.
    day_of_week: int | None = Field(None, ge=0, le=6)
    hour_start: int = Field(0, ge=0, le=23)
    hour_end: int = Field(-1, ge=-1, le=23)


@router.post("/maintenance-windows")
def create_maintenance_window(req: MaintenanceWindowIn) -> dict[str, Any]:
    """Create a maintenance window to suppress a recurring false-positive."""
    from backend.core.state import StateDB

    try:
        with StateDB() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS maintenance_windows (
                    id INTEGER PRIMARY KEY, app_key TEXT NOT NULL,
                    check_name TEXT NOT NULL, label TEXT NOT NULL DEFAULT 'Scheduled task',
                    day_of_week INTEGER, hour_start INTEGER NOT NULL,
                    hour_end INTEGER NOT NULL DEFAULT -1, enabled INTEGER NOT NULL DEFAULT 1,
                    created_at INTEGER NOT NULL DEFAULT (unixepoch())
                )""")
            db.execute(
                """INSERT INTO maintenance_windows
                   (app_key, check_name, label, day_of_week, hour_start, hour_end)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    req.app_key,
                    req.check_name,
                    req.label,
                    req.day_of_week,
                    req.hour_start,
                    req.hour_end,
                ),
            )
    except Exception as _e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to create maintenance window: {type(_e).__name__}: {_e}",
        ) from _e
    return {"ok": True}


@router.delete("/maintenance-windows/{window_id}")
@limiter.limit("10/minute")  # type: ignore[untyped-decorator]  # light mutation — maintenance window delete (id=467)
def delete_maintenance_window(request: Request, window_id: int) -> dict[str, Any]:
    """Remove a maintenance window."""
    from backend.core.state import StateDB

    try:
        with StateDB() as db:
            db.execute("DELETE FROM maintenance_windows WHERE id = ?", (window_id,))
            # NOTE: StateDB auto-commits on __exit__ — db._c.commit() removed (Core Rule 4.4)
    except Exception as _e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to delete maintenance window: {type(_e).__name__}: {_e}",
        ) from _e
    return {"ok": True}


# ── Source availability (Tier 1 + 2) ─────────────────────────────────────


@router.get("/sources")
def get_source_availability() -> dict[str, Any]:
    """Return current source availability for all registered external resources."""
    from backend.core.state import StateDB
    import json as _j

    try:
        from backend.health.source_checker import _ensure_tables

        _ensure_tables()
    except Exception as e:
        log.debug("health API best-effort step skipped: %s", e)
    try:
        with StateDB() as db:
            rows = db.execute("""
                SELECT source_type, resource_key, url, status,
                       http_status, error, last_checked
                FROM source_availability
                ORDER BY
                    CASE status WHEN 'missing' THEN 0 WHEN 'unreachable' THEN 1 ELSE 2 END,
                    resource_key
            """).fetchall()
            last_scan = db.get_setting("source_scan_last_at")
            summary_raw = db.get_setting("source_scan_summary")
        items = [dict(r) for r in rows]
        issues = [i for i in items if i["status"] != "ok"]
        summary = _j.loads(summary_raw) if summary_raw else {}
        return {
            "items": items,
            "issues": issues,
            "last_scan_at": int(last_scan) if last_scan else None,
            "summary": summary,
        }
    except Exception as _e:
        return {"items": [], "issues": [], "last_scan_at": None, "summary": {}, "error": str(_e)}


@router.post("/sources/scan")
@limiter.limit("10/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped; triggers external source-availability fetches (#1205 external-fetch tier)
async def trigger_source_scan(request: Request) -> dict[str, Any]:
    """Trigger an immediate source availability scan (async, non-blocking)."""
    import asyncio
    from backend.health.source_checker import run_source_scan

    try:
        _task = asyncio.create_task(run_source_scan(), name="source-scan-manual")
        _background_tasks.add(_task)
        _task.add_done_callback(_background_tasks.discard)
        return {"ok": True, "message": "Source scan started in background."}
    except Exception as _e:
        raise HTTPException(status_code=500, detail=f"Failed to start source scan: {_e}") from _e


class ReplacementRequest(BaseModel):
    source_type: str
    resource_key: str
    url: str


@router.post("/sources/find-replacement")
@limiter.limit("5/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped; LLM replacement-lookup external call, heaviest (#1205 external-fetch tier)
async def find_source_replacement(request: Request, req: ReplacementRequest) -> dict[str, Any]:
    """Ask the LLM to find a replacement for a missing/broken source URL.

    Returns a suggestion with confidence score. Never auto-applies —
    user must confirm via the /sources/apply-replacement endpoint.
    """
    try:
        from backend.health.source_checker import find_replacement

        result = await find_replacement(req.source_type, req.resource_key, req.url)
        return result
    except Exception as _e:
        raise HTTPException(status_code=500, detail=f"Replacement lookup failed: {_e}") from _e


class ApplyReplacementRequest(BaseModel):
    source_type: str
    resource_key: str
    old_url: str
    new_url: str


@router.post("/sources/apply-replacement")
@limiter.limit("10/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped; applies a confirmed replacement, DB mutation (#1205 mutation tier)
def apply_source_replacement(request: Request, req: ApplyReplacementRequest) -> dict[str, Any]:
    """Apply a confirmed URL replacement.

    For docker_image: updates the apps table image + image_tag.
    For hf_model: updates the recommended_models cache (frontend only —
    manifest URLs require a code change).
    """
    from backend.core.state import StateDB

    if req.source_type == "docker_image":
        # Parse new_url into image + tag
        if ":" in req.new_url.split("/")[-1]:
            new_image, new_tag = req.new_url.rsplit(":", 1)
        else:
            new_image, new_tag = req.new_url, "latest"

        try:
            with StateDB() as db:
                db.execute(
                    "UPDATE apps SET image=?, image_tag=? WHERE key=?",
                    (new_image, new_tag, req.resource_key),
                )
                # Mark source as ok with new URL
                db.execute(
                    """
                    INSERT INTO source_availability
                        (source_type, resource_key, url, status, last_checked)
                    VALUES (?, ?, ?, 'ok', unixepoch())
                    ON CONFLICT(source_type, resource_key, url)
                    DO UPDATE SET status='ok', last_checked=unixepoch()
                """,
                    (req.source_type, req.resource_key, req.new_url),
                )
                # Mark old URL as superseded
                db.execute(
                    """
                    UPDATE source_availability
                    SET status='superseded', error='Replaced by user'
                    WHERE source_type=? AND resource_key=? AND url=?
                """,
                    (req.source_type, req.resource_key, req.old_url),
                )
                # NOTE: StateDB auto-commits on __exit__ — db._c.commit() removed (Core Rule 4.4)
        except Exception as _e:
            raise HTTPException(
                status_code=500,
                detail=f"Failed to apply source replacement: {type(_e).__name__}: {_e}",
            ) from _e
        return {"ok": True, "message": f"Image updated to {req.new_url}. Restart app to apply."}

    elif req.source_type == "hf_model":
        # Can't auto-patch code — tell user what to do
        return {
            "ok": False,
            "message": (
                f"HuggingFace model URLs are defined in the catalog. "
                f"The suggested URL is: {req.new_url} — "
                f"use it in the custom URL download field on the Models page."
            ),
            "suggested_url": req.new_url,
        }

    return {"ok": False, "message": f"Unknown source type: {req.source_type}"}


# ── Pending fixes API ─────────────────────────────────────────────────────


class _EscalateRequest(BaseModel):
    app_key: str
    check_name: str
    problem: str
    logs: str = ""
    context: str = ""


@router.get("/pending-fixes")
def get_pending_fixes() -> list[dict[str, Any]]:
    """Return all pending AI-suggested fixes awaiting user approval."""
    from backend.core.state import StateDB

    with StateDB() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS pending_fixes (
                id INTEGER PRIMARY KEY, app_key TEXT NOT NULL,
                check_name TEXT NOT NULL, action_type TEXT NOT NULL,
                problem TEXT NOT NULL, suggested_fix TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                status TEXT NOT NULL DEFAULT 'pending', model TEXT,
                created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                resolved_at INTEGER,
                UNIQUE(app_key, check_name, action_type)
            )""")
        rows = db.execute(
            "SELECT * FROM pending_fixes WHERE status='pending' ORDER BY confidence DESC, created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


@router.post("/pending-fixes/{fix_id}/approve")
async def approve_fix(fix_id: int) -> dict[str, Any]:
    """Approve and execute a pending AI fix."""
    from backend.core.state import StateDB
    from backend.core.ai_safety import execute_action
    import time as _t

    try:
        with StateDB() as db:
            # Ensure table exists (created lazily in checker.py, may not exist yet)
            db.execute("""CREATE TABLE IF NOT EXISTS pending_fixes (
                id INTEGER PRIMARY KEY, app_key TEXT NOT NULL,
                check_name TEXT NOT NULL, action_type TEXT NOT NULL,
                problem TEXT NOT NULL, suggested_fix TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                status TEXT NOT NULL DEFAULT 'pending', model TEXT,
                created_at INTEGER NOT NULL DEFAULT (unixepoch()),
                resolved_at INTEGER,
                UNIQUE(app_key, check_name, action_type))""")
            row = db.execute("SELECT * FROM pending_fixes WHERE id=?", (fix_id,)).fetchone()
    except Exception as _dbe:
        raise HTTPException(status_code=500, detail=f"Database error: {_dbe}") from _dbe
    if not row:
        raise HTTPException(status_code=404, detail=f"Fix {fix_id} not found")
    fix = dict(row)
    try:
        result = await execute_action(
            fix["action_type"],
            fix["app_key"],
            fix["suggested_fix"],
            approved=True,  # user explicitly clicked "Approve" for this fix
            caller_context="health_api_approve_fix",
        )
    except Exception as _e:
        raise HTTPException(status_code=500, detail=f"Action execution failed: {_e}") from _e
    # Determine outcome accurately
    if result.get("executed"):
        outcome = "success"
    elif result.get("requires_approval"):
        # Safety tier is 'suggest' — user approved but action needs manual run
        outcome = "user_approved_manual"
    else:
        outcome = "pending"

    # Enrich result with the fix command when manual execution is needed
    if result.get("requires_approval"):
        result["manual_command"] = fix.get("suggested_fix", "")
        result["message"] = (
            f"Manual action required — run this command:\n{fix.get('suggested_fix', '')}"
        )

    # Record the outcome through the single agent↔API seam (#822) so it carries
    # the signature_hash/diagnosis_class the learning store keys on (the same
    # recipe the next identical failure recomputes for a cache hit).
    from backend.agent.fix_outcome import record_fix_outcome

    with StateDB() as db:
        # Insert fix_history FIRST, then record its id on the pending_fixes row —
        # the #822 referential link (so verification stamps the right row by id).
        fix_history_id = record_fix_outcome(
            db,
            app_key=fix["app_key"],
            problem=fix["problem"],
            error_type=fix["action_type"],
            context=fix["problem"],
            suggested_fix=fix["suggested_fix"],
            outcome=outcome,
        )
        db.execute(
            "UPDATE pending_fixes SET status='approved', resolved_at=?, fix_history_id=? WHERE id=?",
            (int(_t.time()), fix_history_id, fix_id),
        )
    # Post-fix verification: re-check the container after a delay. Runs as a
    # SUPERVISED asyncio task (#996 — replaces an unjoinable daemon thread that
    # slept + ran a real docker inspect unobservably). It stamps the LINKED
    # fix_history row by id (#822 Unit B). See backend.health.fix_verification.
    if result.get("executed"):
        schedule_fix_verification(fix["app_key"], fix_history_id)

    return result


@router.post("/pending-fixes/{fix_id}/reject")
def reject_fix(
    fix_id: int, reason: str | None = None,
) -> dict[str, Any]:
    """Reject a pending AI fix. Returns 404 if fix not found.

    A rejection is recorded through the #822 seam as outcome='failure' (it IS
    negative learning signal — it demotes the fix's score via learning_outcome_tally).
    An optional free-text ``reason`` is captured in ``fix_history.rejection_reason``
    and fed to the LLM via ``context_assembler._section_fix_history`` so the agent
    can learn from user feedback (#1164 rejection-learning).
    """
    from backend.core.state import StateDB
    import time as _t

    with StateDB() as db:
        try:
            row = db.execute("SELECT * FROM pending_fixes WHERE id=?", (fix_id,)).fetchone()
        except Exception:
            row = None
        if not row:
            raise HTTPException(status_code=404, detail=f"Fix {fix_id} not found")
        fix = dict(row)
        db.execute(
            "UPDATE pending_fixes SET status='rejected', resolved_at=? WHERE id=?",
            (int(_t.time()), fix_id),
        )
        try:
            # A rejection IS learning signal (the suggestion was judged wrong) —
            # record it through the seam (#822) so it carries signature_hash and
            # is visible to learning_outcome_tally. The prior inline INSERT left
            # signature_hash NULL, so every rejection was invisible to learning.
            from backend.agent.fix_outcome import record_fix_outcome

            fix_history_id = record_fix_outcome(
                db,
                app_key=fix["app_key"],
                problem=fix["problem"],
                error_type=fix["action_type"],
                context=fix["problem"],
                suggested_fix=fix["suggested_fix"],
                outcome="failure",
                rejection_reason=reason or "",
            )
            # #822 referential link: record which fix_history row this reject produced.
            db.execute(
                "UPDATE pending_fixes SET fix_history_id=? WHERE id=?",
                (fix_history_id, fix_id),
            )
        except Exception as e:
            log.debug("health API best-effort step skipped: %s", e)
    return {"ok": True}


@router.post("/escalate")
async def escalate_to_cloud(req: _EscalateRequest) -> dict[str, Any]:
    """Escalate a complex diagnosis to the fastest available cloud LLM.

    Used when local model returns low confidence or 'escalate' action type.
    Tries Groq → Cerebras → OpenRouter in order of speed.
    """
    import httpx as _hx
    import json as _ej
    from backend.core.state import StateDB
    from backend.health.context_assembler import assemble_context

    with StateDB() as db:
        cfg = _ej.loads(db.get_setting("llm_agent_config") or "{}")
    api_key = cfg.get("api_key", "")
    provider = cfg.get("provider", "ollama")

    # Build escalation prompt with full context
    ctx = assemble_context(req.app_key, req.check_name)
    prompt = f"""You are an expert homelab systems administrator. A local AI agent was unable to diagnose this issue with high confidence and has escalated to you.

App: {req.app_key}
Check: {req.check_name}
Problem reported: {req.problem}
Recent logs:
{req.logs[-1500:] if req.logs else "(none)"}
{ctx}

Provide a thorough diagnosis. Respond ONLY with JSON:
{{"problem": "clear one-sentence description", "root_cause": "detailed root cause analysis", "suggested_fix": "exact command or step-by-step action", "action": "restart_container|reload_config|pull_image|rewire|restart_managed_service|remount_storage|manual", "confidence": 0.0, "escalation_notes": "what the local model likely missed"}}"""

    # Try cloud providers in speed order: groq → cerebras → openrouter
    CLOUD_ENDPOINTS = [
        ("groq", "https://api.groq.com/openai/v1/chat/completions", "llama-3.3-70b-versatile"),
        ("cerebras", "https://api.cerebras.ai/v1/chat/completions", "llama-3.3-70b"),
        (
            "openrouter",
            "https://openrouter.ai/api/v1/chat/completions",
            "meta-llama/llama-3.3-70b-instruct:free",
        ),
    ]

    # Use configured provider's key if it's a cloud provider, else try all
    for ep_provider, url, model in CLOUD_ENDPOINTS:
        if not api_key:
            continue
        if provider != ep_provider and provider not in ("ollama", "llamacpp", "shimmy", "localai"):
            continue  # use configured key only for configured provider
        try:
            async with _hx.AsyncClient(timeout=30) as client:
                resp = await client.post(
                    url,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "HTTP-Referer": "https://github.com/SLOP-Platform/SLOP",
                        "X-Title": "SLOP Health Agent Escalation",
                    },
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "response_format": {"type": "json_object"},
                    },
                )
                if resp.status_code == 200:
                    raw = resp.json()["choices"][0]["message"]["content"]
                    data: dict[str, Any] = _ej.loads(raw)
                    data["escalated_to"] = f"{ep_provider}/{model}"
                    return data
        except Exception as e:
            # this cloud provider failed — try the next endpoint in the list
            log.debug("escalation provider %s failed: %s", ep_provider, e)
            continue

    return {
        "problem": req.problem,
        "root_cause": "Cloud escalation unavailable — no API keys configured.",
        "suggested_fix": "Configure a cloud provider API key in Settings → AI / LLM → Inference provider.",
        "action": "manual",
        "confidence": 0.0,
        "escalated_to": None,
    }


class LLMTestRequest(BaseModel):
    """Typed request body for POST /llm-test (id=466)."""

    provider: str = Field("ollama", description="Provider key: ollama, openai, groq, etc.")
    api_key: str = Field("", description="API key for cloud providers")
    model: str = Field("", description="Model name/ID to test")
    ollama_url: str = Field("http://localhost:11434", description="Ollama base URL")


@router.post("/llm-test")
@limiter.limit("5/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped; external LLM call to a user-supplied URL (#1205 external-fetch tier)
async def llm_test(request: Request, req: LLMTestRequest) -> dict[str, Any]:
    """Test an LLM provider config with a minimal prompt.
    Returns latency, model used, and whether the response was valid JSON.
    """
    import time as _time

    provider = req.provider
    api_key = req.api_key
    model = req.model
    ollama_url = req.ollama_url

    test_prompt = (
        "You are a JSON API. Respond with exactly this JSON and nothing else: "
        '{"status": "ok", "message": "SLOP LLM connection test passed"}'
    )

    start = _time.monotonic()
    try:
        if provider == "ollama":
            # SSRF floor (#1193 — this site was missed): ollama_url is user-supplied, so a
            # hostile value could aim this server-side fetch at cloud-metadata (169.254.169.254)
            # or link-local. Literal-only (httpx resolves at connect; same as registry/models).
            try:
                assert_not_metadata_url(ollama_url, resolve_dns=False)
            except UrlNotAllowed:
                return {
                    "ok": False,
                    "error": "ollama_url points at a disallowed address (cloud-metadata/link-local).",
                    "latency_ms": 0,
                }
            async with pinned_async_client(timeout=15) as client:
                r = await client.post(
                    f"{ollama_url}/api/generate",
                    json={
                        "model": model or "phi4-mini",
                        "prompt": test_prompt,
                        "stream": False,
                        "format": "json",
                    },
                )
            raw = r.json().get("response", "")
        else:
            from backend.core.cloud_llm import PROVIDERS as _CP

            p = _CP.get(provider, {})
            base = p.get("base_url", "").rstrip("/")
            if not base:
                return {"ok": False, "error": f"Unknown provider: {provider}", "latency_ms": 0}
            hdrs = {
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/SLOP-Platform/SLOP",
                "X-Title": "SLOP LLM Test",
            }
            if provider == "anthropic":
                hdrs["anthropic-version"] = "2023-06-01"
            m = model or p.get("default_model", "")
            rf = {} if provider == "anthropic" else {"response_format": {"type": "json_object"}}
            async with pinned_async_client(timeout=20) as client:
                r = await client.post(
                    f"{base}/chat/completions",
                    headers=hdrs,
                    json={
                        "model": m,
                        "messages": [{"role": "user", "content": test_prompt}],
                        "max_tokens": 100,
                        **rf,
                    },
                )
            raw = r.json().get("choices", [{}])[0].get("message", {}).get("content", "")

        elapsed = int((_time.monotonic() - start) * 1000)
        import json as _jj

        try:
            _fenced = (
                raw.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
            )
            parsed = _jj.loads(_fenced)
            valid = parsed.get("status") == "ok"
        except Exception:
            valid = False

        return {
            "ok": valid,
            "latency_ms": elapsed,
            "model": model,
            "raw": raw[:200],
            "error": "" if valid else "Response was not valid JSON or unexpected format",
        }

    except Exception as e:
        return {
            "ok": False,
            "latency_ms": int((_time.monotonic() - start) * 1000),
            "model": model,
            "raw": "",
            "error": safe_detail(e, "LLM probe failed.", log=log),
        }


@router.get("/llm-providers")
def llm_providers() -> dict[str, Any]:
    """Return all available LLM providers with metadata for the Settings UI."""
    from backend.core.cloud_llm import PROVIDERS

    # Model IDs are not hardcoded — providers release new models frequently.
    # Future: fetch live from each provider's /v1/models endpoint (tracked in
    # GitHub issues). For now, all providers use a free-text entry.
    MODELS: dict[str, list[dict[str, Any]]] = {
        "groq": [
            {
                "id": "",
                "label": "Enter model ID — see console.groq.com/docs/models",
                "recommended": True,
            }
        ],
        "cerebras": [
            {
                "id": "",
                "label": "Enter model ID — see inference-docs.cerebras.ai",
                "recommended": True,
            }
        ],
        "openrouter": [
            {"id": "", "label": "Enter model ID — see openrouter.ai/models", "recommended": True}
        ],
        "mistral": [
            {
                "id": "",
                "label": "Enter model ID — see docs.mistral.ai/getting-started/models",
                "recommended": True,
            }
        ],
        "cohere": [
            {
                "id": "",
                "label": "Enter model ID — see docs.cohere.com/docs/models",
                "recommended": True,
            }
        ],
        "google": [
            {
                "id": "",
                "label": "Enter model ID — see ai.google.dev/gemini-api/docs/models",
                "recommended": True,
            }
        ],
        "anthropic": [
            {
                "id": "",
                "label": "Enter model ID — see console.anthropic.com/docs",
                "recommended": True,
            }
        ],
        "openai": [
            {
                "id": "",
                "label": "Enter model ID — see platform.openai.com/docs/models",
                "recommended": True,
            }
        ],
    }

    result = {}
    for key, meta in PROVIDERS.items():
        result[key] = {
            **meta,
            "key": key,
            "models": MODELS.get(
                key,
                [{"id": meta.get("default_model", ""), "label": "Default", "recommended": True}],
            ),
        }

    return {"providers": result}


@router.get("/apps/{key}/container-status")
def get_container_status(key: str) -> dict[str, Any]:
    """Lightweight container health poll for the wizard install progress view.

    Returns current Docker state without triggering a full health cycle.
    Frontend polls this every 3s during install to show live progress.
    """
    import subprocess as _sp

    try:
        r = _sp.run(
            ["docker", "inspect", "--format", "{{.State.Status}}|{{.State.Health.Status}}", key],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:
            return {"key": key, "status": "missing", "health": "unknown", "ready": False}
        parts = r.stdout.strip().split("|")
        container_status = parts[0] if parts else "unknown"
        health = parts[1] if len(parts) > 1 else "none"
        # "none" health means no healthcheck defined — treat running as healthy
        ready = container_status == "running" and health in ("healthy", "none", "")
        return {
            "key": key,
            "status": container_status,
            "health": health,
            "ready": ready,
        }
    except Exception as e:
        return {
            "key": key,
            "status": "error",
            "health": "unknown",
            "ready": False,
            "error": safe_detail(e, "Could not read container status.", log=log),
        }
