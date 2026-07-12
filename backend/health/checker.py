"""backend/health/checker.py

Health check runner — Step 7 core.

Runs manifest-defined health checks against installed apps.
On failure: attempts self-heal, invokes LLM agent if available,
sends ntfy notification, and disables app if performance thresholds exceeded.

Design:
  - Health checks run on a schedule (configurable, default 30s)
  - Each check is independent — one failing app doesn't block others
  - The LLM agent enriches failures but is never in the critical path
  - disable_app() is only called for ENHANCEMENT criticality apps
    or when PERF_THRESHOLDS are exceeded for other apps
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import httpx

from backend.core.logging import get_logger
from backend.core.state import StateDB
from backend.core.url_guard_httpx import pinned_async_client
from backend.health import notifiers
from backend.health.swallow_counter import record_swallow
from backend.manifests.executor import (
    PERF_THRESHOLDS,
    Criticality,
    get_criticality,
)
from backend.manifests.loader import load_manifest

# LLM integration — extracted to checker_llm.py (file-size discipline)
from backend.health.checker_llm import (
    _llm_state as _llm_state,
    _LLM_ACTION_MAP as _LLM_ACTION_MAP,
    _log_routing as _log_routing,
    _check_ram_for_llm as _check_ram_for_llm,
    _load_provider_config as _load_provider_config,
    _call_ollama as _call_ollama,
    _call_cloud_provider as _call_cloud_provider,
    _call_openai_compatible as _call_openai_compatible,
    _dispatch_llm_call as _dispatch_llm_call,
    _maybe_rag_expand as _maybe_rag_expand,
    _track_llm_success as _track_llm_success,
    _classify_llm_error as _classify_llm_error,
    _persist_pending_fix as _persist_pending_fix,
    _llm_diagnose as _llm_diagnose,
)

log = get_logger(__name__)


# `_dispatch_llm_call` is the single canonical impl in checker_llm.py (re-exported
# above). It is the ONE cloud-egress choke point — defined where the `_call_*`
# helpers + `scrub` live so there is no per-module scrub-drift class (#1156).
# `check_llm_outbound_scrubbed` (ADR-0021) AST-verifies that single def scrubs.


def _build_diagnosis_prompt(app_key: str, check_result: Any, logs: str) -> str:
    """Assemble the full LLM prompt: failure summary + DB context + RAG enrichment."""
    try:
        from backend.health.context_assembler import assemble_context as _ctx

        _diagnostic_ctx = _ctx(app_key, check_result.check_name)
    except Exception:
        _diagnostic_ctx = ""

    prompt = f"""You are a homelab health agent diagnosing a failing service. Respond with JSON ONLY — no prose, no markdown.

=== FAILURE SUMMARY ===
App: {app_key}
Check: {check_result.check_name}
Error: {check_result.message}
Response time: {check_result.response_time_ms:.0f}ms

=== RECENT DOCKER LOGS ===
{logs[-2000:]}

{_diagnostic_ctx}

=== CONTEXT READING RULES ===
- If "MASS FAILURE EVENT" appears: set action=escalate, confidence=0.9, explain infra root cause
- If "TRAEFIK IS STOPPED" or "TRAEFIK CONTAINER MISSING" appears: set action=manual, explain Traefik is the cause
- If "MANAGED SERVICES DOWN: postgres" or "redis" appears: set action=restart_managed_service
- If "CRITICAL: No compose fragment" appears: set action=manual, suggested_fix="Reinstall from Catalog"
- If "OOM killed: YES" appears: set action=restart_container, confidence=0.95
- If "Already pending user approval" appears: acknowledge it, do NOT suggest the same action again
- If "INSTALL FAILED" appears: app never ran, likely config/image issue — set action=pull_image or manual
- If "DOCKER DAEMON SLOW" appears: note this may be a false positive, set confidence ≤0.5
- If error contains "401" or "403" and "INFRA DEGRADED" appears in context: the auth middleware (tinyauth/authelia) is down. Set action=manual, cause="auth middleware down", confidence=0.95. Do NOT diagnose the individual app — fix the infra first.
- If error contains "502" or "504" and Traefik-related: set action=manual, cause="Traefik routing failed", check Traefik container status
- Avoid suggesting actions listed under "Previous fix attempts" that show [✓] (already worked)
- Avoid suggesting actions listed under "Previous fix attempts" that show [✗] (already failed)

=== AVAILABLE ACTIONS (pick the most specific one) ===
restart_container      — container crashed or is stuck, needs restart
reload_config          — config file changed, service needs reload (not full restart)
pull_image             — image outdated or corrupted, pull fresh copy
rewire                 — API key/URL to another app is wrong or stale
restart_managed_service — postgres or redis is down
remount_storage        — NFS/rclone mount is stale or disconnected
manual                 — requires human action (reinstall, fix config, check hardware)
escalate               — local model uncertain, needs cloud LLM review

=== CONFIDENCE CALIBRATION ===
≥0.90 — single clear cause with direct evidence in logs or context
0.70-0.89 — likely cause, some supporting evidence
0.50-0.69 — plausible but multiple possible causes
<0.50 — insufficient evidence, recommend escalate

Respond with exactly this JSON and nothing else:
{{"problem": "one sentence describing what is failing", "cause": "one sentence root cause from logs/context", "suggested_fix": "specific command or step — not a generic suggestion", "action": "one of the 8 action types above", "confidence": 0.0}}"""

    try:
        from backend.core.rag import enrich_prompt_with_context as _rag_enrich

        error_context = f"{app_key} {check_result.check_name} {check_result.message} {logs[-500:]}"
        prompt = _rag_enrich(prompt, error_context)
    except Exception as e:
        # RAG is optional — proceed without it
        log.debug("RAG prompt enrichment skipped: %s", e)
    return prompt


def _extract_diagnosis(data: dict[str, Any]) -> tuple[str, str, str, float]:
    """Map LLM JSON to (action_type, problem, suggested, confidence)."""
    raw_action = data.get("action", "manual").lower()
    action_type = _LLM_ACTION_MAP.get(raw_action, raw_action)
    confidence = float(data.get("confidence", 0.5))
    problem = data.get("problem", "")
    cause = data.get("cause", "")
    if cause and cause.lower() not in problem.lower():
        problem = f"{problem} (Root cause: {cause})" if problem else cause
    suggested = data.get("suggested_fix", "")
    escalation = data.get("escalation_notes", "")
    if escalation:
        suggested = (
            f"{suggested} [Escalation context: {escalation[:200]}]" if suggested else escalation
        )
    return action_type, problem, suggested, confidence


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CheckResult:
    app_key: str
    check_name: str
    ok: bool
    message: str
    response_time_ms: float = 0.0
    detail: str = ""
    llm_diagnosis: str | None = None
    action_type: str | None = (
        None  # restart_container | reload_config | pull_image | rewire | restart_managed_service | remount_storage | manual | escalate
    )
    auto_healed: bool = False
    notification_sent: bool = False


@dataclass
class HealthRun:
    started_at: float
    results: list[CheckResult] = field(default_factory=list)
    apps_checked: int = 0
    apps_healthy: int = 0
    apps_degraded: int = 0
    apps_disabled: int = 0
    llm_agent_state: str = "unknown"


# ---------------------------------------------------------------------------
# HTTP health check
# ---------------------------------------------------------------------------


async def _check_http(
    app_key: str,
    check_name: str,
    base_url: str,
    path: str,
    expect_status: int = 200,
    timeout: float = 10.0,
) -> CheckResult:
    url = f"{base_url.rstrip('/')}{path}"
    start = time.monotonic()
    try:
        async with pinned_async_client(timeout=timeout) as client:
            resp = await client.get(url)
        elapsed_ms = (time.monotonic() - start) * 1000
        ok = resp.status_code == expect_status
        if ok:
            _msg = f"HTTP {resp.status_code}"
        elif resp.status_code == 401:
            _msg = "API authentication required — open the app to complete setup"
        elif resp.status_code == 403:
            _msg = "Access forbidden (403) — check app authentication settings"
        else:
            _msg = f"Expected {expect_status}, got {resp.status_code}"
        return CheckResult(
            app_key=app_key,
            check_name=check_name,
            ok=ok,
            message=_msg,
            response_time_ms=elapsed_ms,
        )
    except httpx.TimeoutException:
        elapsed_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            app_key=app_key,
            check_name=check_name,
            ok=False,
            message=f"Request timed out after {timeout}s",
            response_time_ms=elapsed_ms,
        )
    except Exception as e:
        elapsed_ms = (time.monotonic() - start) * 1000
        return CheckResult(
            app_key=app_key,
            check_name=check_name,
            ok=False,
            message=f"Connection failed: {type(e).__name__}",
            response_time_ms=elapsed_ms,
            detail=str(e)[:200],
        )


async def _check_tcp(app_key: str, check_name: str, port: int) -> CheckResult:
    """TCP reachability check — verifies port accepts connections on localhost.

    Distinguishes:
      - Port not bound at all (wrong config, app crashed before binding)
      - Port bound but refusing connection (race condition / startup)
      - Connected successfully
    """
    import socket as _sock
    import subprocess as _sp

    if not port:
        return CheckResult(
            app_key=app_key,
            check_name=check_name,
            ok=False,
            message="TCP check: no port configured.",
        )
    start = time.monotonic()
    try:
        with _sock.create_connection(("localhost", port), timeout=5):
            elapsed = (time.monotonic() - start) * 1000
            return CheckResult(
                app_key=app_key,
                check_name=check_name,
                ok=True,
                message=f"TCP port {port} open",
                response_time_ms=elapsed,
            )
    except (ConnectionRefusedError, OSError, TimeoutError) as e:
        # Item 5: distinguish "port not bound" from "connection refused"
        # ss -tlnp shows listening ports — if absent, the process hasn't bound
        try:
            ss = _sp.run(["ss", "-tlnp"], capture_output=True, text=True, timeout=3)
            port_bound = f":{port} " in ss.stdout or f":{port}\t" in ss.stdout
        except Exception:
            port_bound = None  # can't tell
        if port_bound is False:
            msg = (
                f"Port {port} is not bound — process may have crashed before startup "
                f"or is configured with the wrong port. Check: docker logs {app_key}"
            )
        else:
            msg = f"TCP port {port} unreachable: {e}"
        return CheckResult(app_key=app_key, check_name=check_name, ok=False, message=msg)


def _check_process(app_key: str, check_name: str, container_name: str) -> CheckResult:
    """Check that the container process is running via docker inspect."""
    import subprocess as _sp

    try:
        r = _sp.run(
            ["docker", "inspect", "--format", "{{json .State.Running}}", container_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0 and r.stdout.strip() == "true":
            return CheckResult(
                app_key=app_key,
                check_name=check_name,
                ok=True,
                message=f"Container {container_name} is running",
            )
        return CheckResult(
            app_key=app_key,
            check_name=check_name,
            ok=False,
            message=f"Container {container_name} is not running",
        )
    except Exception as e:
        return CheckResult(
            app_key=app_key,
            check_name=check_name,
            ok=False,
            message=f"Could not inspect container: {e}",
        )


# ---------------------------------------------------------------------------
# ntfy notification
# ---------------------------------------------------------------------------


async def _send_notification(
    title: str,
    message: str,
    priority: str = "default",
    ntfy_url: str = "http://ntfy:80",
    topic: str = "slop",
) -> bool:
    """Send a push notification via the active notifier (ntfy by default).

    Thin dispatcher over :mod:`backend.health.notifiers` (#989): the active
    provider is selected by the ``notifier_provider`` setting, defaulting to
    ntfy. The ``ntfy_url`` / ``topic`` params are honored by the (default) ntfy
    provider, preserving the historical threaded-config behavior and this
    function's pinned signature. Returns a bool so callers (health alerts, the
    agent kill-switch audit) know whether delivery succeeded — a failure is
    surfaced as ``False``, never swallowed.
    """
    sent = await notifiers.dispatch(title, message, priority, ntfy_url=ntfy_url, ntfy_topic=topic)
    return bool(sent)


# ---------------------------------------------------------------------------
# Self-heal
# ---------------------------------------------------------------------------


# Manifest action aliases — module-scope so tests can introspect.
_HEAL_ALIASES: dict[str, str] = {
    "restart": "restart_container",
    "restart_container": "restart_container",
    "reload": "reload_config",
    "reload_config": "reload_config",
    "pull": "pull_image",
    "pull_image": "pull_image",
    "rewire": "rewire",
    "remount": "remount_storage",
    "remount_storage": "remount_storage",
    "restart_managed_service": "restart_managed_service",
}


def _heal_restart_container(app_key: str) -> bool:
    import subprocess as _sub

    from backend.agent.safe_update import _capture_container_state, _rollback_to_state

    prior = _capture_container_state(app_key)
    if prior is None:
        log.warning("Self-heal restart aborted for %s: snapshot capture failed", app_key)
        return False
    r = _sub.run(["docker", "restart", "-t", "10", app_key], capture_output=True, timeout=30)
    if r.returncode == 0:
        log.info("Self-healed '%s' via manifest restart", app_key)
        return True
    log.warning("Self-heal restart failed for %s: %s", app_key, r.stderr.decode()[:100])
    _rollback_to_state(app_key, prior)
    return False


def _heal_reload_config(app_key: str) -> bool:
    import subprocess as _sub

    from backend.agent.safe_update import _capture_container_state, _rollback_to_state

    prior = _capture_container_state(app_key)
    if prior is None:
        log.warning("Self-heal reload_config aborted for %s: snapshot capture failed", app_key)
        return False
    r = _sub.run(["docker", "kill", "--signal=HUP", app_key], capture_output=True, timeout=10)
    if r.returncode == 0:
        log.info("Self-healed '%s' via manifest reload_config", app_key)
        return True
    _rollback_to_state(app_key, prior)
    return False


def _heal_pull_image(app_key: str) -> bool:
    """Self-heal ``pull_image`` — backed by the SAFE-update discipline.

    Previously this did a blind ``docker pull`` and stopped (no restart, no
    verify, no rollback).  It now routes through ``safe_update`` which pulls,
    restarts, verifies health, and ROLLS BACK to the prior image on a failed
    verify — the per-app analog of the platform's ``ms-update`` SAFE update.

    Because a SAFE update restarts the container (disruptive), it is gated by
    :func:`backend.agent.safe_update.evaluate_update_gate`:

      * SUPERVISED (default) — the agent PROPOSES; it does not execute. We log
        the proposal and return False (no autonomous mutation).
      * AUTONOMOUS + per-app auto-update preference on — execute the SAFE update.

    Returns True only when an executed SAFE update verified healthy on the new
    image.
    """
    from backend.agent.safe_update import (
        evaluate_update_gate,
        safe_update_container,
    )
    from backend.agent.types import OperationalLevel

    with StateDB() as _db:
        _app = _db.get_app(app_key)
        _level_raw = _db.get_setting("agent_operational_level")
    if not (_app and _app.image):
        return False

    _level = OperationalLevel.from_setting(_level_raw)
    _gate = evaluate_update_gate(app_key, _level)
    if not _gate["execute"]:
        # PROPOSE-not-execute: never trigger a disruptive update silently.
        log.info(
            "Self-heal pull_image for '%s' PROPOSED (not executed): %s", app_key, _gate["reason"]
        )
        return False

    image_ref = f"{_app.image}:{_app.image_tag or 'latest'}"
    log.info("Self-heal pull_image for '%s' EXECUTING SAFE update: %s", app_key, _gate["reason"])
    result = safe_update_container(app_key, image_ref)
    if result["ok"]:
        log.info("Self-healed '%s' via SAFE pull_image: %s", app_key, result["message"])
        return True
    log.warning(
        "Self-heal SAFE pull_image for '%s' did not succeed: %s", app_key, result["message"]
    )
    return False


def _heal_rewire(app_key: str) -> bool:
    with StateDB() as _db:
        _stale = _db.execute(
            """SELECT w.id FROM wiring w
               JOIN apps a1 ON a1.id = w.source_app_id
               WHERE a1.key = ? AND w.status IN ('stale','failed')""",
            (app_key,),
        ).fetchall()
        for row in _stale:
            _db.execute("UPDATE wiring SET status='pending' WHERE id=?", (row["id"],))
    log.info("Self-healed '%s' via manifest rewire (%d entries)", app_key, len(_stale))
    return len(_stale) > 0


def _heal_remount_storage(app_key: str) -> bool:
    import subprocess as _sub

    from backend.agent.safe_update import _capture_container_state, _rollback_to_state

    with StateDB() as _db:
        _stores = _db.execute(
            "SELECT name, source_type FROM storage_sources WHERE status='error'"
        ).fetchall()
    for s in _stores:
        cname = f"rclone-{s['name'].lower().replace(' ', '-')}"
        prior = _capture_container_state(cname)
        if prior is None:
            log.warning(
                "Self-heal remount_storage aborted for rclone '%s': snapshot capture failed",
                cname,
            )
            continue
        r = _sub.run(["docker", "restart", cname], capture_output=True, timeout=15)
        if r.returncode != 0:
            _rollback_to_state(cname, prior)
    log.info("Self-healed '%s' via manifest remount_storage", app_key)
    return bool(_stores)


def _heal_restart_managed_service(app_key: str) -> bool:
    import subprocess as _sub

    from backend.agent.safe_update import _capture_container_state, _rollback_to_state

    with StateDB() as _db:
        _dep = _db.execute(
            """SELECT ms.container_name FROM app_dependencies d
               JOIN apps a ON a.id = d.app_id
               JOIN managed_services ms ON ms.service_type = d.dependency_type
               WHERE a.key = ? AND ms.status = 'error' LIMIT 1""",
            (app_key,),
        ).fetchone()
    if not _dep:
        return False
    container_name = _dep["container_name"]
    prior = _capture_container_state(container_name)
    if prior is None:
        log.warning(
            "Self-heal restart_managed_service aborted for %s: snapshot capture failed",
            container_name,
        )
        return False
    r = _sub.run(["docker", "restart", container_name], capture_output=True, timeout=30)
    if r.returncode == 0:
        log.info("Self-healed '%s' via manifest restart_managed_service", app_key)
        return True
    _rollback_to_state(container_name, prior)
    return False


# Action dispatch table — module-scope so tests can introspect.
_HEAL_DISPATCHERS: dict[str, Callable[[str], bool]] = {
    "restart_container": _heal_restart_container,
    "reload_config": _heal_reload_config,
    "pull_image": _heal_pull_image,
    "rewire": _heal_rewire,
    "remount_storage": _heal_remount_storage,
    "restart_managed_service": _heal_restart_managed_service,
}


async def _attempt_self_heal(
    app_key: str,
    action: str,
    check_result: CheckResult,
) -> bool:
    """Attempt the self-heal action defined in the manifest.

    IMPORTANT: Manifest self_heal bypasses the AI safety tier. When a user
    defines self_heal in their manifest, they are explicitly opting into
    automatic remediation for that specific action. The safety tier (suggest/act)
    applies only to LLM-suggested fixes sent to pending_fixes for approval.

    Returns True if the action was successfully executed.

    Step 2.7.a: action dispatch is now table-driven via `_HEAL_ALIASES`
    (manifest-string → canonical-action) + `_HEAL_DISPATCHERS`
    (canonical-action → handler). Drops cyclomatic complexity from 16 to ≤ 4.

    Gate scope: pre-execution log + DB record are written here (the single
    autonomous dispatch point) before ANY handler fires. READ-ONLY actions
    and LLM calls are exempt.
    Fail-closed: if the DB write fails, the heal action does NOT proceed.
    """
    action_type = _HEAL_ALIASES.get(action, action)
    handler = _HEAL_DISPATCHERS.get(action_type)
    if handler is None:
        return False

    condition = check_result.message if check_result else ""
    log.info(
        "AGENT-ACTION: trigger=self_heal app=%s action=%s condition=%s",
        app_key,
        action_type,
        condition,
    )
    with StateDB() as db:
        op_id = db.log_operation(
            operation="self_heal",
            subject_type="app",
            subject_key=app_key,
            triggered_by="agent",
            detail={"action": action_type, "condition": condition},
        )

    op_status = "completed"
    op_error: str | None = None
    try:
        result = handler(app_key)
        if not result:
            op_status = "failed"
        return result
    except Exception as e:
        op_status = "failed"
        op_error = str(e)
        log.warning("Self-heal failed for %s/%s: %s", app_key, action_type, e)
        return False
    finally:
        try:
            with StateDB() as db:
                db.complete_operation(op_id, status=op_status, error=op_error)
        except Exception as _fin_e:
            log.warning("Self-heal: complete_operation failed for op_id=%s: %s", op_id, _fin_e)


# ---------------------------------------------------------------------------
# Main check runner
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Container startup grace period
# ---------------------------------------------------------------------------

_DOCKER_DEFAULT_GRACE_S = 120  # seconds after container start to skip health checks


def _container_started_at(container_name: str) -> float | None:
    """Return Unix timestamp when container last started, or None if unavailable."""
    import subprocess

    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.StartedAt}}", container_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        # Docker returns ISO 8601: "2026-05-01T02:59:14.123456789Z"
        from datetime import datetime

        raw = r.stdout.strip().replace("Z", "+00:00")
        # Handle nanoseconds — Python datetime only handles 6 decimal places
        import re

        raw = re.sub(r"(\.\d{6})\d+", r"\1", raw)
        dt = datetime.fromisoformat(raw)
        return dt.timestamp()
    except Exception:
        return None


def _in_startup_grace(container_name: str, grace_s: int) -> tuple[bool, float]:
    """Return (is_in_grace, seconds_since_start).

    Returns (True, age) if the container started less than grace_s seconds ago.
    Returns (False, age) otherwise. Returns (False, -1) if we can't determine.
    """
    started_at = _container_started_at(container_name)
    if started_at is None:
        return False, -1.0
    age = time.time() - started_at
    return age < grace_s, age


def _container_runtime_state(container_name: str) -> dict[str, Any]:
    """Fetch runtime diagnostic data from docker inspect.

    Returns dict with: restart_count, exit_code, oom_killed, finished_at.
    Never raises — returns empty dict on any failure.
    """
    import subprocess
    import json as _json

    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{json .State}}", container_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return {}
        state = _json.loads(r.stdout.strip())
        return {
            "restart_count": state.get("RestartCount", 0),
            "exit_code": state.get("ExitCode", 0),
            "oom_killed": state.get("OOMKilled", False),
            "finished_at": state.get("FinishedAt", ""),
        }
    except Exception:
        return {}


def _config_disk_pct(config_path: str | None) -> int | None:
    """Return used % of the filesystem holding config_path, or None."""
    if not config_path:
        return None
    import shutil

    try:
        u = shutil.disk_usage(config_path)
        return int(u.used / u.total * 100)
    except Exception:
        return None


async def _container_net_reachability(
    container_name: str,
    wired_targets: list[tuple[str, int]],  # [(hostname, port), ...]
) -> dict[str, bool]:
    """Quick TCP reachability test from inside the container to its wired deps.
    Uses `docker exec <ctr> nc -z -w2 <host> <port>`.
    Only runs if wired_targets is non-empty. Never raises.
    """
    import asyncio

    results: dict[str, bool] = {}
    if not wired_targets:
        return results

    async def _probe(host: str, port: int) -> tuple[str, bool]:
        key = f"{host}:{port}"
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "exec",
                container_name,
                "nc",
                "-z",
                "-w2",
                host,
                str(port),
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            await asyncio.wait_for(proc.wait(), timeout=5)
            return key, proc.returncode == 0
        except Exception:
            return key, False

    tasks = [_probe(h, p) for h, p in wired_targets[:4]]  # max 4 probes
    for coro in asyncio.as_completed(tasks):
        key, ok = await coro
        results[key] = ok
    return results


# ── check_app helpers — split for complexity discipline (Core Rule 8.1) ─────


def _reconcile_stale_failed() -> int:
    """Flip apps from 'failed' to 'running' when their container is now healthy.

    Handles the race between SLOP's post-install health-wait timeout and an
    app's Docker start_period: if the container became healthy after the wait
    expired, the stale 'failed' status is corrected on the next health cycle.
    Only flips when container health is confirmed (healthy/none) — leaves
    'starting' alone until a subsequent cycle can verify.
    """
    import time as _t
    from backend.core import docker_client as _dc

    flipped = 0
    with StateDB() as _db:
        stale = _db.get_all_apps(status="failed")
    for _app in stale:
        try:
            _cname = getattr(_app, "container_name", None) or _app.key
            _info = _dc.get_container(_cname)
            if _info and _info.status == "running" and _info.health in ("healthy", "none", ""):
                with StateDB() as _db:
                    _db.upsert_app(_app.key, status="running", last_healthy_at=int(_t.time()))
                log.info(
                    "Reconciled stale-failed '%s' → running (container healthy)",
                    _app.key,
                )
                flipped += 1
        except Exception as _e:
            log.debug("Stale-failed reconciliation for '%s': %s", _app.key, _e)
    return flipped


def _check_infra_app(app_key: str, app_record: Any | None = None) -> None:
    """Tier-0 (infra) app fallback — docker SDK health check.

    Writes results directly to DB; returns nothing. Used when the app
    record exists in the DB but has no catalog manifest (e.g. tunnel
    providers, auth proxy).
    """
    import time as _itime
    from backend.core import docker_client as _dc

    # Use stored container_name (may differ from app_key for infra providers)
    if app_record is not None:
        _cname = getattr(app_record, "container_name", None) or app_key
    else:
        with StateDB() as _idb:
            _app_rec = _idb.get_app(app_key)
        _cname = (getattr(_app_rec, "container_name", None) or app_key) if _app_rec else app_key
    try:
        _info = _dc.get_container(_cname)
        if _info is not None:
            _ok = _info.status == "running" and _info.health not in ("unhealthy",)
            _status = "ok" if _ok else "error"
            _msg = f"Container {_info.status}" + (
                f" (health: {_info.health})" if _info.health and _info.health != "none" else ""
            )
            with StateDB() as _idb:
                _idb.upsert_health_check(
                    "app",
                    app_key,
                    "container_state",
                    status=_status,
                    summary=_msg,
                )
                if _ok:
                    _idb.upsert_app(app_key, status="running", last_healthy_at=int(_itime.time()))
                else:
                    _idb.upsert_app(app_key, status="failed")
        else:
            with StateDB() as _idb:
                _idb.upsert_health_check(
                    "app",
                    app_key,
                    "container_state",
                    status="error",
                    summary="Container not found — may not be deployed",
                )
                _idb.upsert_app(app_key, status="failed")
    except Exception as _ie:
        log.debug("Infra app health check failed for %s: %s", app_key, _ie)


def _load_manifest_or_skip(app_key: str) -> Any | None:
    """Load the catalog manifest, falling back to infra-app docker-inspect.

    Returns the manifest object on success. Returns None when:
      - app is unmanaged (no manifest, not in apps table) → caller returns []
      - app is infra (tier=0) → infra check has been performed; caller returns []
    """
    try:
        return load_manifest(app_key)
    except Exception:
        with StateDB() as _tdb:
            _tier_app = _tdb.get_app(app_key)
        if not _tier_app or getattr(_tier_app, "tier", 1) != 0:
            return None  # truly unmanaged
        _check_infra_app(app_key, _tier_app)
        return None


def _precheck_fragment(app_key: str, app: Any, manifest: Any) -> list[CheckResult] | None:
    """Pre-check 1: compose fragment must exist on disk.

    Returns a list of error CheckResults (one per defined health check)
    when the fragment is missing — the LLM can't help here. Returns None
    when the fragment is fine and the caller should continue.
    """
    try:
        from backend.core.config import config as _cfg

        _frag_path = _cfg.compose_dir / f"{app_key}.yaml"
        if not _frag_path.exists() and app and app.status not in ("failed", "disabled", "removing"):
            log.warning("App '%s' has no compose fragment — misconfigured", app_key)
            return [
                CheckResult(
                    app_key=app_key,
                    check_name=check_def.name,
                    ok=False,
                    message=(
                        f"No compose fragment for '{app_key}' — app cannot start. "
                        f"Remove from DB or reinstall."
                    ),
                )
                for check_def in manifest.health_checks
            ]
    except Exception as e:
        log.debug("best-effort health-check step skipped: %s", e)
    return None


def _precheck_oom(
    app_key: str, manifest: Any, runtime_state: dict[str, Any], in_grace: bool
) -> list[CheckResult] | None:
    """Pre-check 2: OOM kill detection.

    Returns a single oom_killed CheckResult when the container was OOM-killed
    (and not in grace). Returns None to signal the caller should continue.
    """
    if runtime_state.get("oom_killed") and not in_grace:
        log.warning("App '%s' was OOM killed", app_key)
        _oom_result = CheckResult(
            app_key=app_key,
            check_name="oom_killed",
            ok=False,
            message=(
                f"{manifest.display_name} was killed due to out-of-memory (OOM). "
                f"Restart count: {runtime_state.get('restart_count', '?')}. "
                f"Increase Docker memory limit or reduce concurrent apps."
            ),
        )
        _oom_result.action_type = "restart_container"
        _oom_result.llm_diagnosis = (
            "[OOM KILL | confidence=95%] Container killed by kernel OOM killer. "
            "Suggested fix: Add mem_limit to compose fragment or reduce app memory usage."
        )
        return [_oom_result]
    return None


def _grace_results(
    app_key: str, manifest: Any, container_age: float, grace_s: int
) -> list[CheckResult]:
    """Return 'starting' CheckResults for every defined health check.

    Used when the container is inside the configured startup grace period —
    real health checks would always fail during boot.
    """
    log.debug(
        "%s is in startup grace period (started %.0fs ago, grace=%ds) — skipping checks",
        app_key,
        container_age,
        grace_s,
    )
    return [
        CheckResult(
            app_key=app_key,
            check_name=check_def.name,
            ok=True,
            message=f"Starting — {container_age:.0f}s into {grace_s}s grace period",
        )
        for check_def in manifest.health_checks
    ]


async def _run_one_check(
    app_key: str, check_def: Any, base_url: str, host_port: int | None, container_name: str
) -> CheckResult:
    """Dispatch a single check_def to its check_type (http/tcp/process/custom)."""
    if check_def.check_type == "http" and base_url:
        return await _check_http(
            app_key=app_key,
            check_name=check_def.name,
            base_url=base_url,
            path=check_def.path or "/",
            expect_status=check_def.expect_status,
        )
    if check_def.check_type == "tcp":
        tcp_port = check_def.port or host_port or 0
        return await _check_tcp(app_key, check_def.name, tcp_port)
    if check_def.check_type == "process":
        return _check_process(app_key, check_def.name, container_name)
    return CheckResult(
        app_key=app_key,
        check_name=check_def.name,
        ok=True,
        message=f"Check type '{check_def.check_type}' not implemented.",
    )


def _resolve_pending_fixes(app_key: str, check_name: str) -> None:
    """Mark pending_fixes for this (app,check) as resolved on a passing check.

    Also updates fix_history outcome=success for any pending fixes for the app.
    """
    try:
        import time as _ft

        with StateDB() as _fdb:
            _pending = _fdb.execute(
                """SELECT id FROM pending_fixes
                   WHERE app_key=? AND check_name=? AND status='pending'""",
                (app_key, check_name),
            ).fetchall()
            for _pf in _pending:
                _fdb.execute(
                    "UPDATE pending_fixes SET status='resolved', resolved_at=? WHERE id=?",
                    (int(_ft.time()), _pf["id"]),
                )
            _fdb.execute(
                """UPDATE fix_history SET outcome='success'
                   WHERE app_key=? AND outcome='pending'""",
                (app_key,),
            )
    except Exception as e:
        log.debug("best-effort health-check step skipped: %s", e)


def _record_healthy(app_key: str, check_name: str, result: CheckResult) -> None:
    """Persist a passing health-check outcome."""
    with StateDB() as db:
        db.upsert_health_check(
            "app",
            app_key,
            check_name,
            status="ok",
            summary=f"OK ({result.response_time_ms:.0f}ms)",
        )


async def _try_self_heal(
    app_key: str, manifest: Any, check_def: Any, result: CheckResult
) -> Any | None:
    """Walk manifest.self_heal entries looking for a match for this check.

    On match: invoke the heal action and update result.auto_healed; returns the
    matched entry. With no match but a non-empty list, returns the LAST entry
    (preserves the original loop-variable semantics for the auto_fix recording
    in `_record_unhealthy`). Returns None when self_heal is empty.
    """
    last = None
    for heal in manifest.self_heal:
        last = heal
        if heal.condition == check_def.name or heal.condition in (result.message or ""):
            healed = await _attempt_self_heal(app_key, heal.action, result)
            result.auto_healed = healed
            return heal
    return last


def _mass_failure_diagnosis(app_key: str, result: CheckResult) -> bool:
    """If ≥4 apps are currently failing, mark this result as escalation.

    Returns True when the short-circuit fired — caller should skip per-app
    LLM diagnosis, notification, and DB recording (preserves the original
    `continue` semantics so mass-failure results are not double-recorded).
    """
    try:
        with StateDB() as _mdb:
            _mass = _mdb.execute(
                """SELECT COUNT(DISTINCT subject_key) as n FROM health_checks
                   WHERE status IN ('error','warning')
                   AND checked_at >= ?""",
                (int(time.time()) - 300,),
            ).fetchone()
        if _mass and _mass["n"] >= 4 and app_key not in ("postgres", "redis", "traefik"):
            result.llm_diagnosis = (
                f"[INFRASTRUCTURE EVENT | confidence=90%] "
                f"{_mass['n']} apps failing simultaneously — likely shared root cause. "
                f"Check: managed services (postgres/redis), storage mounts, "
                f"Docker daemon health, and Traefik status before diagnosing {app_key} individually."
            )
            result.action_type = "escalate"
            return True
    except Exception as e:
        log.debug("best-effort health-check step skipped: %s", e)
    return False


def _filter_error_logs(app_key: str) -> str:
    """Pull recent docker logs and keep only error/warn lines (signal over noise).

    Falls back to the raw 2KB tail when no error keywords match. Returns
    a placeholder string when logs are unreachable.
    """
    try:
        from backend.core import docker_client

        raw_logs = docker_client.container_logs(app_key, tail=200)
        error_lines = [
            line
            for line in raw_logs.splitlines()
            if any(
                k in line.lower()
                for k in ("error", "warn", "exception", "fatal", "panic", "killed", "oom")
            )
        ]
        if error_lines:
            return "\n".join(error_lines[-20:])
        return raw_logs[-2000:]
    except Exception:
        return "(logs unavailable)"


async def _collect_net_checks(app_key: str, container_name: str) -> dict[str, bool]:
    """Probe TCP reachability from inside the container to its wired deps."""
    _net_targets: list[tuple[str, int]] = []
    try:
        with StateDB() as _ndb:
            _wires = _ndb.execute(
                """SELECT a2.container_name, a2.web_port
                   FROM wiring w
                   JOIN apps a1 ON a1.id = w.source_app_id
                   JOIN apps a2 ON a2.id = w.target_app_id
                   WHERE a1.key = ? AND w.status = 'active'""",
                (app_key,),
            ).fetchall()
        _net_targets = [
            (w["container_name"] or "", w["web_port"] or 0) for w in _wires if w["web_port"]
        ]
    except Exception as e:
        log.debug("best-effort health-check step skipped: %s", e)
    return await _container_net_reachability(container_name, _net_targets)


async def _diagnose_with_llm(
    app_key: str,
    container_name: str,
    runtime_state: dict[str, Any],
    result: CheckResult,
    ollama_url: str,
) -> bool:
    """Mass-failure short-circuit + filtered-logs + net-checks + LLM call.

    Returns True iff the mass-failure short-circuit fired (caller skips
    notification + recording — preserves original `continue` semantics).
    """
    if _mass_failure_diagnosis(app_key, result):
        return True
    logs = _filter_error_logs(app_key)
    runtime_state["network_checks"] = await _collect_net_checks(app_key, container_name)

    from backend.core.llm_router import best_model_for as _best_model

    _model_rec = _best_model("reasoning")
    _model_name = (
        _model_rec.ollama_name or _model_rec.filename.replace(".gguf", "")
        if _model_rec
        else "phi4-mini"
    )
    result.llm_diagnosis = await _llm_diagnose(
        app_key,
        result,
        logs,
        ollama_url=ollama_url,
        model=_model_name,
    )
    import json as _rj

    result.detail = _rj.dumps(
        {
            "restart_count": runtime_state.get("restart_count"),
            "exit_code": runtime_state.get("exit_code"),
            "oom_killed": runtime_state.get("oom_killed"),
            "config_disk_pct": runtime_state.get("config_disk_pct"),
            "network_checks": runtime_state.get("network_checks", {}),
        }
    )
    return False


async def _notify_failure(
    app_key: str, manifest: Any, result: CheckResult, ntfy_url: str, ntfy_topic: str
) -> None:
    """Send the 'unhealthy' or 'auto-fixed' ntfy notification for a failing check."""
    title = f"{'🔧 Auto-fixed' if result.auto_healed else '⚠️ Unhealthy'}: {manifest.display_name}"
    msg_parts = [f"Check: {result.check_name}", f"Error: {result.message}"]
    if result.llm_diagnosis:
        msg_parts.append(f"Diagnosis: {result.llm_diagnosis}")
    if result.auto_healed:
        msg_parts.append("Container restarted automatically.")
    sent = await _send_notification(
        title=title,
        message="\n".join(msg_parts),
        priority="high" if not result.auto_healed else "default",
        ntfy_url=ntfy_url,
        topic=ntfy_topic,
    )
    result.notification_sent = sent


async def _notify_escalation(
    app_key: str, result: CheckResult, ntfy_url: str, ntfy_topic: str
) -> None:
    """Dispatch a best-effort ntfy notification when action_type='escalate'.

    Called when a mass-failure short-circuit or low-confidence LLM diagnosis
    produces action_type='escalate'.  Non-blocking — logs at WARNING on send
    failure but never raises.
    """
    confidence_str = ""
    if result.llm_diagnosis and "confidence=" in result.llm_diagnosis:
        # Extract confidence label already embedded in llm_diagnosis string.
        confidence_str = result.llm_diagnosis.split("confidence=")[1].split("]")[0]
    msg_parts = [
        f"App: {app_key}",
        f"Problem: {result.message}",
    ]
    if confidence_str:
        msg_parts.append(f"Confidence: {confidence_str}")
    if result.llm_diagnosis:
        msg_parts.append(f"Diagnosis: {result.llm_diagnosis}")
    try:
        sent = await _send_notification(
            title=f"🚨 Escalation required: {app_key}",
            message="\n".join(msg_parts),
            priority="urgent",
            ntfy_url=ntfy_url,
            topic=ntfy_topic,
        )
    except Exception as e:
        # Best-effort: honour the "never raises" contract even if the sender misbehaves.
        log.warning("escalation ntfy dispatch raised for %s (best-effort): %s", app_key, e)
        sent = False
    if not sent:
        log.warning("escalation ntfy dispatch failed for %s (best-effort)", app_key)
    else:
        log.info("escalation notification sent for %s", app_key)
    result.notification_sent = sent


def _record_unhealthy(
    app_key: str, manifest: Any, check_def: Any, result: CheckResult, heal_entry: Any | None
) -> None:
    """Persist a failing/auto-fixed health-check outcome and history row."""
    _hc_status = "warning" if not result.auto_healed else "ok"
    with StateDB() as db:
        db.upsert_health_check(
            "app",
            app_key,
            check_def.name,
            status=_hc_status,
            summary=result.message,
            auto_fix=heal_entry.action if (manifest.self_heal and heal_entry) else None,
        )
        if _hc_status in ("error", "warning"):
            try:
                import time as _t

                db.execute(
                    "INSERT INTO health_check_history "
                    "(subject_type,subject_key,check_name,status,summary,checked_at) "
                    "VALUES ('app',?,?,?,?,?)",
                    (app_key, check_def.name, _hc_status, result.message[:500], int(_t.time())),
                )
            except Exception as e:
                log.debug("best-effort health-check step skipped: %s", e)


async def _maybe_perf_warn(
    app_key: str, manifest: Any, result: CheckResult, ntfy_url: str, ntfy_topic: str
) -> None:
    """Send a performance-disable nudge for slow ENHANCEMENT-tier apps."""
    criticality = get_criticality(app_key)
    slow = result.response_time_ms > PERF_THRESHOLDS["api_response_seconds"] * 1000
    if slow and criticality == Criticality.ENHANCEMENT and not result.auto_healed:
        log.warning(
            "%s response %.0fms exceeds threshold — offering disable",
            app_key,
            result.response_time_ms,
        )
        await _send_notification(
            title=f"⏱️ Performance: {manifest.display_name} is slow",
            message=(
                f"{manifest.display_name} responded in {result.response_time_ms:.0f}ms "
                f"(threshold: {PERF_THRESHOLDS['api_response_seconds'] * 1000:.0f}ms). "
                f"Consider disabling it to free resources."
            ),
            priority="default",
            ntfy_url=ntfy_url,
            topic=ntfy_topic,
        )


async def check_app(
    app_key: str, ollama_url: str, ntfy_url: str, ntfy_topic: str
) -> list[CheckResult]:
    """Run all health checks for a single app — orchestrator over the helpers above.

    Step 4.1 wire-up: thin timing wrapper around the real implementation
    in `_check_app_inner`. Outcome label is `ok` when every CheckResult
    is ok (or no checks ran), otherwise `error`.
    """
    from backend.core.metrics import health_check_duration_seconds

    _t0 = time.monotonic()
    results: list[CheckResult] = []
    try:
        results = await _check_app_inner(
            app_key,
            ollama_url,
            ntfy_url,
            ntfy_topic,
        )
        return results
    finally:
        outcome = "ok" if all(r.ok for r in results) else "error"
        health_check_duration_seconds.labels(
            app_key=app_key,
            outcome=outcome,
        ).observe(time.monotonic() - _t0)


async def _dispatch_wiring_completeness(app_key: str, manifest: Any, app: Any) -> None:
    """Wiring completeness check — runs on every health cycle per app.

    Detects missing or failed wiring rows and triggers remediation. Wraps
    _check_wiring_completeness to keep _check_app_inner within complexity budget.
    """
    import asyncio as _aio2

    try:
        wiring_actions = await _aio2.to_thread(_check_wiring_completeness, app_key, manifest, app)
        for action in wiring_actions:
            if action.get("type") == "rewire":
                log.info(
                    "Wiring completeness: failed row %s→%s (%s) — scheduling rewire",
                    app_key,
                    action.get("target"),
                    action.get("wire_type"),
                )
                await _attempt_self_heal(
                    app_key,
                    "rewire",
                    CheckResult(
                        app_key=app_key,
                        check_name="wiring_completeness",
                        ok=False,
                        message=(
                            f"Wiring failed: {app_key}→"
                            f"{action.get('target')} "
                            f"({action.get('wire_type')})"
                        ),
                    ),
                )
            elif action.get("type") == "notice":
                log.warning(
                    "Wiring completeness notice for %s: %s",
                    app_key,
                    action.get("message"),
                )
    except Exception as _we:
        log.debug("Wiring completeness check failed for %s: %s", app_key, _we)
        record_swallow("checker.wiring_completeness_check")


def _check_wiring_completeness(app_key: str, manifest: Any, app: Any) -> list[dict[str, Any]]:
    """Check wiring completeness for an app.

    For each wire step in the manifest's post_deploy list:
    - If the wiring row is missing entirely, trigger run_wiring_pass to create it.
    - If the wiring row has status='failed', return a rewire action for catalog
      apps or a notice for community apps.

    Returns a list of action dicts (may be empty).
    """
    actions: list[dict[str, Any]] = []
    app_source = getattr(app, "manifest_source", "catalog") or "catalog"

    for step in getattr(manifest, "post_deploy", []):
        if getattr(step, "step_type", "") != "wire":
            continue
        wire_type = getattr(step, "wire_type", "")
        target = getattr(step, "target", "")
        if not target:
            continue

        try:
            with StateDB() as db:
                source_app_rec = db.get_app(app_key)
                target_app_rec = db.get_app(target)
            if not source_app_rec or not target_app_rec:
                continue  # target not installed yet — skip silently

            with StateDB() as db:
                row = db.execute(
                    "SELECT id, status FROM wiring "
                    "WHERE source_app_id=? AND target_app_id=? AND wire_type=?",
                    (source_app_rec.id, target_app_rec.id, wire_type),
                ).fetchone()

            if row is None:
                # Row is missing — trigger a wiring pass to create it.
                log.info(
                    "Wiring completeness: missing row %s→%s (%s) — triggering pass",
                    app_key,
                    target,
                    wire_type,
                )
                from backend.manifests.executor import run_wiring_pass

                run_wiring_pass({app_key})
            elif row["status"] == "failed":
                if app_source == "catalog":
                    actions.append(
                        {
                            "type": "rewire",
                            "app": app_key,
                            "target": target,
                            "wire_type": wire_type,
                        }
                    )
                else:
                    actions.append(
                        {
                            "type": "notice",
                            "message": (f"Wiring incomplete: {app_key}→{target} ({wire_type})"),
                            "action": "rewire",
                        }
                    )
        except Exception as e:
            log.debug("Wiring completeness check skipped for %s: %s", app_key, e)
            record_swallow("checker.wiring_completeness_step")

    return actions


async def _check_app_inner(
    app_key: str,
    ollama_url: str,
    ntfy_url: str,
    ntfy_topic: str,
) -> list[CheckResult]:
    """Real check_app body — wrapped by check_app() above for timing."""
    results: list[CheckResult] = []

    import asyncio as _aio

    manifest = await _aio.to_thread(_load_manifest_or_skip, app_key)
    if manifest is None:
        return results

    with StateDB() as db:
        app = db.get_app(app_key)
    if not app or app.status in ("disabled", "removing", "installing"):
        return results

    _host_port = getattr(app, "host_port", None) or getattr(manifest, "web_port", None)
    base_url = f"http://localhost:{_host_port}" if _host_port else ""

    frag_results = _precheck_fragment(app_key, app, manifest)
    if frag_results is not None:
        return frag_results

    container_name = (app.container_name or app_key) if app else app_key
    grace_s = int(
        getattr(manifest, "health_grace_s", None)
        or getattr(manifest, "start_grace_s", 0)
        or _DOCKER_DEFAULT_GRACE_S
    )
    # Run blocking subprocess/Docker calls in a thread pool so the event loop
    # is not stalled while waiting for 'docker inspect'.  Previously these ran
    # directly on the event loop thread, blocking all API responses during the
    # health cycle (14 apps x 2 subprocess calls x ~100ms = ~2.8s of stall).
    in_grace, container_age = await _aio.to_thread(_in_startup_grace, container_name, grace_s)

    runtime_state = await _aio.to_thread(_container_runtime_state, container_name)
    runtime_state["config_disk_pct"] = _config_disk_pct(
        getattr(app, "config_path", None) if app else None
    )

    oom_results = _precheck_oom(app_key, manifest, runtime_state, in_grace)
    if oom_results is not None:
        return oom_results

    if in_grace and container_age >= 0:
        return _grace_results(app_key, manifest, container_age, grace_s)

    for check_def in manifest.health_checks:
        result = await _run_one_check(app_key, check_def, base_url, _host_port, container_name)

        if result.ok:
            _resolve_pending_fixes(app_key, check_def.name)
            _record_healthy(app_key, check_def.name, result)
            results.append(result)
            continue

        # Failure path
        heal_entry = await _try_self_heal(app_key, manifest, check_def, result)

        if _llm_state.get("status") not in ("disabled",):
            mass_failure = await _diagnose_with_llm(
                app_key, container_name, runtime_state, result, ollama_url
            )
            if mass_failure:
                # action_type='escalate' set by _mass_failure_diagnosis — dispatch notification.
                await _notify_escalation(app_key, result, ntfy_url, ntfy_topic)
                continue  # skip per-app recording + append (mass-failure semantics)

        await _notify_failure(app_key, manifest, result, ntfy_url, ntfy_topic)
        _record_unhealthy(app_key, manifest, check_def, result, heal_entry)
        await _maybe_perf_warn(app_key, manifest, result, ntfy_url, ntfy_topic)
        results.append(result)

    await _dispatch_wiring_completeness(app_key, manifest, app)
    return results


async def run_health_cycle(
    ollama_url: str = "http://localhost:11434",
    ntfy_url: str = "http://ntfy:80",
    ntfy_topic: str = "slop",
) -> HealthRun:
    """Run one full health check cycle across all installed apps."""
    run = HealthRun(started_at=time.monotonic())
    run.llm_agent_state = _llm_state.get("status", "unknown")

    # Skip LLM phase if no model is configured or Ollama is not running
    _llm_available = _llm_state.get("status") in ("active", "degraded")
    if not _llm_available:
        log.debug(
            "LLM health agent inactive (status: %s) — "
            "install Ollama and download a model to enable AI health monitoring.",
            _llm_state.get("status", "unknown"),
        )

    # Reconcile apps stuck in 'failed' whose containers became healthy after
    # SLOP's install timeout (F7 — stale status from start_period race).
    # Must run before fetching running_apps so reconciled apps join this cycle.
    try:
        _reconcile_stale_failed()
    except Exception as _rse:
        log.debug("Stale-failed reconciliation: %s", _rse)

    with StateDB() as db:
        running_apps = db.get_all_apps(status="running")
        running_keys = {a.key for a in running_apps}
        # Tier-0 infra apps (cloudflared, tailscale, etc.) may have status="failed"
        # if they were never installed by SLOP — still need container-state checks.
        tier0_extras = [
            a
            for a in db.get_all_apps()
            if a.tier == 0
            and a.key not in running_keys
            and a.status not in ("disabled", "removing")
        ]
        apps = running_apps + tier0_extras

    run.apps_checked = len(apps)

    # Wrap each check_app in a timeout so a single hung app (e.g. DNS resolution
    # stuck, LLM endpoint blocked) can't stall the entire health cycle.
    # 30s per app is generous — http checks have their own 10s timeout internally.
    async def _check_with_timeout(app_key: str) -> list[CheckResult]:
        try:
            return await asyncio.wait_for(
                check_app(app_key, ollama_url=ollama_url, ntfy_url=ntfy_url, ntfy_topic=ntfy_topic),
                timeout=30.0,
            )
        except TimeoutError:
            log.warning("Health check for '%s' timed out after 30s — skipping.", app_key)
            return []

    tasks = [_check_with_timeout(app.key) for app in apps]
    all_results = await asyncio.gather(*tasks, return_exceptions=True)

    _aggregate_app_results(run, all_results)
    await _probe_agent_connectivity(run)
    await _record_process_integrity()
    await _run_spine_self_audit()

    return run


def _aggregate_app_results(run: HealthRun, all_results: list[Any]) -> None:
    """Fold per-app check results into the run tallies."""
    for app_results in all_results:
        if isinstance(app_results, BaseException):
            continue
        for r in app_results:
            run.results.append(r)
            if r.ok or r.auto_healed:
                run.apps_healthy += 1
            else:
                run.apps_degraded += 1


async def _probe_agent_connectivity(run: HealthRun) -> None:
    """Phase B: probe SLOP Agent LLM backend connectivity on every cycle.

    Updates health_checks (subject_type='agent') so /health/summary and
    /health/agent return live status rather than the bootstrap "unknown".

    Also updates _llm_state so /health/llm-agent reflects the current runtime
    state immediately instead of staying "unknown" until an app fails.
    """
    try:
        from backend.core.agent import check_agent_connectivity

        result = await check_agent_connectivity()
        run.llm_agent_state = result
        _llm_state["status"] = "active" if result == "running" else result
        if result == "running":
            _llm_state["consecutive_failures"] = 0
            _llm_state["last_error"] = ""
            _llm_state["last_error_type"] = ""
            import time as _chrono

            _llm_state["last_success_at"] = int(_chrono.time())
    except Exception as _ae:
        log.warning("Agent connectivity check failed: %s", _ae)


async def _record_process_integrity() -> None:
    """Report SLOP's own rule-enforcement coverage as a health dimension."""
    try:
        from backend.agent.integrity import run_process_integrity_check
        from backend.core.agent import AGENT_INTEGRITY_KEY, AGENT_SUBJECT_TYPE_INTEGRITY

        loop = asyncio.get_running_loop()
        integrity = await loop.run_in_executor(None, run_process_integrity_check)
        integrity_status = (
            "critical"
            if integrity.critical_gaps > 0
            else "degraded"
            if integrity.high_gaps > 0
            else "ok"
        )
        try:
            import json as _json

            _detail = _json.dumps(
                {
                    "critical_gaps": integrity.critical_gaps,
                    "high_gaps": integrity.high_gaps,
                    "total_rules": integrity.total_rules,
                }
            )
            with StateDB() as _db:
                _db.upsert_health_check(
                    subject_type=AGENT_SUBJECT_TYPE_INTEGRITY,
                    subject_key=AGENT_INTEGRITY_KEY,
                    check_name="enforcement_coverage",
                    status=integrity_status,
                    summary=integrity.summary,
                    detail=_detail,
                )
        except Exception as _we:
            log.warning("Failed to write process_integrity health check: %s", _we)
    except Exception as _ie:
        log.warning("Process integrity check failed: %s", _ie)


async def _run_spine_self_audit() -> None:
    """GROUND self-audit reconciliation + store-only advisory interpretation.

    Never-raises: a spine failure degrades to a recorded INDETERMINATE finding
    (see spine module). Advisories are NEVER used to trigger automated actions.
    """
    _spine_findings: list[Any] = []
    loop = asyncio.get_running_loop()
    try:
        from backend.agent.spine import persist_findings, run_self_audit_cycle

        _spine_findings = await loop.run_in_executor(
            None, lambda: run_self_audit_cycle(persist=lambda fs: persist_findings(fs, StateDB))
        )
    except Exception as _se:
        log.warning("Self-audit cycle failed: %s", _se)

    # Advisory interpretation. Store-only: guarded by
    # should_run_interpret() (15-min cadence + changed-findings-only). The scrub
    # choke-point is inside spine_egress.send_for_review() which interpret()
    # calls — no second scrub is needed here.
    if _spine_findings:
        try:
            from backend.agent.spine_review import interpret, should_run_interpret

            if should_run_interpret(_spine_findings):
                _annotated = await interpret(
                    _spine_findings,
                    enabled=False,  # Opt-in only; enabled=False is the safe default
                )
                await loop.run_in_executor(
                    None, lambda: _persist_spine_advisories(_annotated, StateDB)
                )
        except Exception as _iae:
            log.warning("Spine advisory interpretation failed: %s", _iae)


def _persist_spine_advisories(findings: list[Any], db_factory: type) -> None:
    """Insert advisory annotations from interpret() into spine_advisories table.

    Store-only — NEVER triggers automated remediation, never writes to
    pending_fixes, never calls apply().  Any DB failure is swallowed
    (best-effort advisory storage must not interrupt the health cycle).
    """
    import json as _json

    try:
        with db_factory() as db:
            for f in findings:
                for ann in getattr(f, "annotations", []):
                    db.execute(
                        "INSERT INTO spine_advisories "
                        "(finding_id, verdict, annotation, provider, created_at) "
                        "VALUES (?, ?, ?, ?, unixepoch())",
                        (
                            str(f.id),
                            str(f.verdict.value) if hasattr(f.verdict, "value") else str(f.verdict),
                            _json.dumps(
                                {
                                    "note": getattr(ann, "note", ""),
                                    "source": getattr(ann, "source", ""),
                                    "raises": (
                                        ann.raises.value if ann.raises is not None else None
                                    ),
                                }
                            ),
                            str(getattr(ann, "source", "llm_review")),
                        ),
                    )
    except Exception as _e:
        log.warning("spine_advisories persist failed: %s", _e)
