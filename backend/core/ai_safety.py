"""backend/core/ai_safety.py

Three-tier AI safety model for the health agent.

  Tier 0 — Observe (always on):
    Read logs, check health, explain errors, query knowledge base.
    Never modifies anything. Safe by default.

  Tier 1 — Suggest (default):
    LLM proposes a fix. User approves before anything changes.
    The suggestion is shown in the health view with Accept/Dismiss.

  Tier 2 — Act (explicit opt-in per action type):
    LLM applies the fix automatically.
    Never enabled by default. Each action type must be explicitly allowed.
    Supported action types (all disabled by default):
      - restart_container   Auto-restart crashed containers
      - reload_config       Send SIGHUP to reload config without restart
      - pull_image          Pull latest image version
    NOT available for auto-act (always requires manual approval):
      - modify_config_file  Too risky — could break the app
      - delete_data         Never automated
      - network_changes     Too broad

The safety level is per-action-type, stored in the settings DB.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from backend.core.logging import get_logger

log = get_logger(__name__)

# Tier 2 (Act) is available for these action types only
ACTABLE_TYPES: set[str] = {
    "restart_container",
    "reload_config",
    "pull_image",
    "rewire",
    "restart_managed_service",
    "remount_storage",
}

# Default safety level — everything starts at Suggest (tier 1)
DEFAULT_SAFETY: dict[str, str] = {
    "restart_container": "suggest",
    "reload_config": "suggest",
    "pull_image": "suggest",
    "rewire": "suggest",
    "restart_managed_service": "suggest",
    "remount_storage": "suggest",
}


def get_safety_level(action_type: str) -> str:
    """Return current safety level for an action type: observe | suggest | act."""
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            val = db.get_setting(f"ai_safety_{action_type}")
            if val in ("observe", "suggest", "act"):
                return val
    except Exception:  # noqa: S110  # best-effort DB lookup; fall back to DEFAULT_SAFETY if DB unavailable
        pass
    return DEFAULT_SAFETY.get(action_type, "suggest")


def set_safety_level(action_type: str, level: str) -> None:
    """Update the safety level for an action type."""
    if level not in ("observe", "suggest", "act"):
        raise ValueError(f"Invalid safety level: {level}. Must be observe, suggest, or act.")
    if level == "act" and action_type not in ACTABLE_TYPES:
        raise ValueError(
            f"Action type '{action_type}' cannot be set to 'act' — "
            f"it always requires manual approval. "
            f"Actable types: {sorted(ACTABLE_TYPES)}"
        )
    from backend.core.state import StateDB

    with StateDB() as db:
        db.set_setting(f"ai_safety_{action_type}", level)
    log.info("AI safety level for '%s' set to '%s'", action_type, level)


def get_all_safety_levels() -> dict[str, dict[str, Any]]:
    """Return all safety levels with metadata for the settings UI."""
    levels = {}
    for action_type in ACTABLE_TYPES | {"modify_config_file", "manual"}:
        current = get_safety_level(action_type)
        levels[action_type] = {
            "level": current,
            "actable": action_type in ACTABLE_TYPES,
            "description": _ACTION_DESCRIPTIONS.get(action_type, ""),
            "can_auto_act": action_type in ACTABLE_TYPES,
        }
    return levels


def should_auto_act(action_type: str) -> bool:
    """Return True only when action type is explicitly set to 'act' tier."""
    if action_type not in ACTABLE_TYPES:
        return False
    return get_safety_level(action_type) == "act"


def should_suggest(action_type: str) -> bool:
    """Return True when action should be suggested to the user."""
    level = get_safety_level(action_type)
    return level in ("suggest", "act")  # act implies suggest was already shown


def _action_restart_container(app_key: str, _detail: str) -> dict[str, Any]:
    import subprocess

    r = subprocess.run(
        ["docker", "restart", app_key],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode == 0:
        msg = f"Container '{app_key}' restarted by AI health agent."
        log.info(msg)
        return {"executed": True, "requires_approval": False, "message": msg}
    return {
        "executed": False,
        "requires_approval": False,
        "message": f"Restart failed: {r.stderr.strip()[:200]}",
    }


def _action_reload_config(app_key: str, _detail: str) -> dict[str, Any]:
    import subprocess

    subprocess.run(
        ["docker", "kill", "--signal=HUP", app_key],
        capture_output=True,
        text=True,
        timeout=10,
    )
    return {
        "executed": True,
        "requires_approval": False,
        "message": f"Config reload signal sent to '{app_key}'.",
    }


def _action_pull_image(app_key: str, _detail: str) -> dict[str, Any]:
    from backend.core.state import StateDB

    with StateDB() as db:
        app = db.get_app(app_key)
    if not (app and app.image):
        return {
            "executed": False,
            "requires_approval": True,
            "message": f"No image recorded for '{app_key}'.",
        }
    import subprocess

    subprocess.run(
        ["docker", "pull", f"{app.image}:{app.image_tag or 'latest'}"],
        capture_output=True,
        text=True,
        timeout=120,
    )
    return {
        "executed": True,
        "requires_approval": False,
        "message": f"Image pulled for '{app_key}'. Restart to apply.",
    }


def _action_rewire(app_key: str, _detail: str) -> dict[str, Any]:
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            stale = db.execute(
                """SELECT w.id, a2.key as target_key
                   FROM wiring w
                   JOIN apps a1 ON a1.id = w.source_app_id
                   JOIN apps a2 ON a2.id = w.target_app_id
                   WHERE a1.key = ? AND w.status IN ('stale','failed')""",
                (app_key,),
            ).fetchall()
            for row in stale:
                db.execute("UPDATE wiring SET status='pending' WHERE id=?", (row["id"],))
        n = len(stale)
        return {
            "executed": True,
            "requires_approval": False,
            "message": (
                f"Marked {n} stale wiring(s) for '{app_key}' as pending — "
                "wiring will re-run on next health cycle."
            ),
        }
    except Exception as _we:
        return {"executed": False, "requires_approval": False, "message": str(_we)}


def _action_restart_managed_service(app_key: str, _detail: str) -> dict[str, Any]:
    """Restart a managed service (postgres/redis) the app depends on,
    when its `managed_services.status='error'`. No-op if no failing
    dependency is recorded."""
    import subprocess
    from backend.core.state import StateDB

    with StateDB() as db:
        dep = db.execute(
            """SELECT ms.container_name FROM app_dependencies d
               JOIN apps a ON a.id = d.app_id
               JOIN managed_services ms ON ms.service_type = d.dependency_type
               WHERE a.key = ? AND ms.status = 'error'
               LIMIT 1""",
            (app_key,),
        ).fetchone()
    if not dep:
        return {
            "executed": False,
            "requires_approval": False,
            "message": "No failing managed service found for this app.",
        }
    cname = dep["container_name"]
    r = subprocess.run(
        ["docker", "restart", cname],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if r.returncode == 0:
        return {
            "executed": True,
            "requires_approval": False,
            "message": f"Managed service '{cname}' restarted.",
        }
    return {
        "executed": False,
        "requires_approval": False,
        "message": f"Failed to restart '{cname}': {r.stderr.strip()[:100]}",
    }


def _action_remount_storage(_app_key: str, _detail: str) -> dict[str, Any]:
    """Restart rclone containers or re-trigger systemd mounts for any
    storage source whose status is currently 'error'. Iterates ALL
    failing sources, not just the caller's app — storage failures are
    cluster-wide so the fix is too."""
    import subprocess
    from backend.core.state import StateDB

    with StateDB() as db:
        bad_stores = db.execute(
            "SELECT name, source_type, mount_point FROM storage_sources WHERE status='error'",
        ).fetchall()
    results = []
    for s in bad_stores:
        if s["source_type"] == "rclone":
            r = subprocess.run(
                ["docker", "restart", f"rclone-{s['name'].lower().replace(' ', '-')}"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            results.append(
                f"{s['name']}: {'restarted' if r.returncode == 0 else 'failed'}",
            )
        else:
            r = subprocess.run(
                [
                    "systemctl",
                    "restart",
                    f"mnt-{s['mount_point'].lstrip('/').replace('/', '\x2d')}.mount",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            results.append(
                f"{s['name']}: {'remounted' if r.returncode == 0 else 'failed'}",
            )
    if not results:
        return {
            "executed": False,
            "requires_approval": False,
            "message": "No failing storage sources found.",
        }
    return {
        "executed": True,
        "requires_approval": False,
        "message": "Storage remount: " + "; ".join(results),
    }


def _action_escalate(app_key: str, _detail: str) -> dict[str, Any]:
    return {
        "executed": True,
        "requires_approval": False,
        "message": (
            f"Cloud LLM escalation noted for '{app_key}'. "
            "A higher-capacity model will be used on the next health cycle."
        ),
    }


def _action_reprovision_hostname(app_key: str, _detail: str) -> dict[str, Any]:
    """Re-register the app's hostname with the configured tunnel
    provider. On failure, returns requires_approval=True so the user
    sees a manual fallback message — the only branch where the
    auto-act path can flip back to manual."""
    try:
        from backend.manifests.executor import _register_app_hostname as _rah
        from backend.manifests.loader import load_manifest as _lmx
        from backend.core.state import StateDB as _SDB

        _mfst = _lmx(app_key)
        with _SDB() as _db:
            _plat = _db.get_platform()
        _rah(app_key, _mfst, _plat)
        return {
            "executed": True,
            "requires_approval": False,
            "message": f"Hostname re-registered for '{app_key}'.",
        }
    except Exception as _re:
        return {
            "executed": False,
            "requires_approval": True,
            "message": (f"Manual: re-register hostname for '{app_key}' with your tunnel. ({_re})"),
        }


# Action aliases — short manifest-form names map to canonical names
# that _ACTION_DISPATCHERS keys on. Drift between these two tables
# would silently break auto-act for the affected action type.
_ACTION_ALIASES: dict[str, str] = {
    "restart_container": "restart_container",
    "reload_config": "reload_config",
    "pull_image": "pull_image",
    "rewire": "rewire",
    "restart_managed_service": "restart_managed_service",
    "remount_storage": "remount_storage",
    "escalate": "escalate",
    "reprovision_hostname": "reprovision_hostname",
    "reprovision": "reprovision_hostname",
}

_ACTION_DISPATCHERS: dict[str, Callable[[str, str], dict[str, Any]]] = {
    "restart_container": _action_restart_container,
    "reload_config": _action_reload_config,
    "pull_image": _action_pull_image,
    "rewire": _action_rewire,
    "restart_managed_service": _action_restart_managed_service,
    "remount_storage": _action_remount_storage,
    "escalate": _action_escalate,
    "reprovision_hostname": _action_reprovision_hostname,
}


# High-impact action types: any action that mutates state or calls
# external services (id=470 — classify conservatively).
# Note: ALL actable types are high-impact by this definition.
_HIGH_IMPACT_TYPES: set[str] = ACTABLE_TYPES | {
    "reprovision_hostname",
    "reprovision",
    "escalate",
}


async def execute_action(
    action_type: str,
    app_key: str,
    detail: str = "",
    approved: bool = False,
    caller_context: str = "",
) -> dict[str, Any]:
    """Execute an AI-suggested action, respecting the safety tier.

    Args:
        action_type:    Canonical or aliased action name.
        app_key:        App the action targets.
        detail:         Human-readable fix description / payload preview.
        approved:       Explicit approval flag — required for high-impact
                        actions when should_auto_act() returns True (id=470).
        caller_context: Short label for the call-site (e.g. "health_api",
                        "auto_heal_cycle") used in pre-execution log entries.

    Returns: {"executed": bool, "message": str, "requires_approval": bool}

    Step 2.7.i: extracts each action branch into its own
    `_action_<name>` helper + an alias-normalising dispatch table
    (`_ACTION_ALIASES` / `_ACTION_DISPATCHERS`). Drops complexity
    from 21 to ≤ 4. Mirrors the 2.7.a `_attempt_self_heal` refactor.
    """
    # ── Pre-execution logging (id=470) ────────────────────────────────────
    # Log every dispatch at INFO level regardless of whether we proceed.
    _payload_preview = detail[:120] + "…" if len(detail) > 120 else detail
    log.info(
        "AI action dispatch",
        action_type=action_type,
        app_key=app_key,
        caller_context=caller_context or "unknown",
        approved=approved,
        payload_preview=_payload_preview,
        high_impact=action_type in _HIGH_IMPACT_TYPES,
    )

    # ── Approval gate for high-impact actions (id=470) ────────────────────
    # A high-impact action dispatched via auto-act (Tier 2) must carry
    # explicit approved=True from the call-site.  Without it the action is
    # rejected with a structured rejection (no exception raised) so callers
    # can surface it cleanly.
    if action_type in _HIGH_IMPACT_TYPES and should_auto_act(action_type) and not approved:
        log.warning(
            "AI action blocked — high-impact action requires approved=True",
            action_type=action_type,
            app_key=app_key,
            caller_context=caller_context or "unknown",
        )
        return {
            "executed": False,
            "requires_approval": True,
            "message": (
                f"High-impact action '{action_type}' for '{app_key}' "
                "requires explicit approval (pass approved=True)."
            ),
        }

    if not should_auto_act(action_type):
        return {
            "executed": False,
            "requires_approval": True,
            "message": f"Action '{action_type}' for '{app_key}' requires user approval.",
        }

    log.warning(
        "AI auto-act: executing '%s' for '%s' — user has opted into Tier 2 for this action type.",
        action_type,
        app_key,
    )

    canonical = _ACTION_ALIASES.get(action_type, action_type)
    handler = _ACTION_DISPATCHERS.get(canonical)
    if handler is None:
        return {
            "executed": False,
            "requires_approval": True,
            "message": (
                f"Action '{action_type}' is not implemented — requires manual intervention."
            ),
        }
    try:
        return handler(app_key, detail)
    except Exception as e:
        log.error("AI auto-act failed for '%s'/'%s': %s", action_type, app_key, e)
        return {"executed": False, "requires_approval": False, "message": str(e)}


_ACTION_DESCRIPTIONS: dict[str, str] = {
    "restart_container": "Restart crashed or unhealthy containers.",
    "reload_config": "Send a reload signal to apply config changes without a full restart.",
    "pull_image": "Pull the latest image version when an update is detected.",
    "rewire": "Re-run wiring between apps when connections go stale.",
    "restart_managed_service": "Restart a failed PostgreSQL or Redis service.",
    "remount_storage": "Re-mount disconnected NAS or cloud storage sources.",
    "modify_config_file": "Change configuration files — always requires manual approval.",
    "manual": "Actions that require human intervention — cannot be automated.",
    "escalate": "Route to a cloud LLM with web search for complex diagnosis.",
}
