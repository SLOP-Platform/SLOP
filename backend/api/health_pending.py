"""backend/api/health_pending.py

Pending-actions computation, extracted from health.py (#1302 linecount drain).

``compute_pending_actions`` derives the operator's outstanding-issues list from
platform DB state, settings, installed apps, and recent health results. health.py
exposes it via a thin ``/pending-actions`` route wrapper and re-exports
``PendingAction`` + ``_env_cache`` (the env-file mtime cache the global test-
isolation registry resets in place). All three symbols were used ONLY by this
function in health.py, so the move is self-contained (no import cycle).
"""

from __future__ import annotations

import time
from typing import Any, cast

from pydantic import BaseModel


class PendingAction(BaseModel):
    priority: str  # error | warning | suggestion
    title: str
    description: str
    action: str  # human-readable action to take
    link: str | None = None  # UI route to navigate to
    icon: str = ""


# Env-file parse cache (mtime-keyed, 30s TTL). Reset in place by the global
# test-isolation registry via backend.api.health._env_cache (re-exported).
_env_cache: dict[str, Any] = {"data": None, "ts": 0.0, "mtime": 0.0}


def _table_exists(db: Any, table_name: str) -> bool:
    try:
        # table_name is only ever passed hardcoded constants by callers, not user input
        db.execute(f"SELECT 1 FROM {table_name} LIMIT 1")  # noqa: S608  # nosec B608
        return True
    except Exception:
        return False


def _read_env_vals(_cfg: Any) -> dict[str, str]:
    """Parse the .env file into a dict, mtime-cached (30s TTL). Empty if absent."""
    if not _cfg.env_file.exists():
        return {}
    try:
        current_mtime = _cfg.env_file.stat().st_mtime
    except OSError:
        current_mtime = 0.0
    now = time.monotonic()
    if (
        _env_cache["data"] is not None
        and _env_cache["mtime"] == current_mtime
        and now - _env_cache["ts"] < 30
    ):
        return cast("dict[str, str]", _env_cache["data"])
    parsed: dict[str, str] = {}
    for line in _cfg.env_file.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            parsed[k.strip()] = v.strip()
    _env_cache["data"] = parsed
    _env_cache["ts"] = now
    _env_cache["mtime"] = current_mtime
    return parsed


def _pending_errors(
    platform: Any, apps: list[Any], env_vals: dict[str, str]
) -> list[PendingAction]:
    """Error-tier issues: platform not configured, missing CF token, apps in error state."""
    out: list[PendingAction] = []

    # Platform not configured
    if platform.status == "pending":
        out.append(
            PendingAction(
                priority="error",
                title="Platform setup incomplete",
                description="The setup wizard has not been completed — Traefik and HTTPS are not configured.",
                action="Run the setup wizard",
                link="/setup",
                icon="\u2699\ufe0f",
            )
        )

    if not env_vals.get("CF_DNS_API_TOKEN") and platform.status == "ready":
        out.append(
            PendingAction(
                priority="error",
                title="Cloudflare API token missing",
                description="CF_DNS_API_TOKEN is not set — wildcard certificates cannot be issued.",
                action="Add it in Settings \u2192 Secrets",
                link="/settings",
                icon="\U0001f511",
            )
        )

    # Apps in error state
    error_apps = [a for a in apps if getattr(a, "status", "") == "error"]
    for app in error_apps[:3]:
        out.append(
            PendingAction(
                priority="error",
                title=f"{app.display_name or app.key} is in error state",
                description="Container failed to start or is unhealthy.",
                action=f"Check logs: ms apps logs {app.key}",
                link="/health",
                icon="\u274c",
            )
        )
    return out


def _pending_warnings(
    auth_slot: Any,
    has_model: bool,
    health_rows: list[Any],
    stale_wiring_rows: list[Any],
    probe_failure_rows: list[Any],
) -> list[PendingAction]:
    """Warning-tier issues: no auth, no LLM, health warnings, stalled wiring, broken probes."""
    out: list[PendingAction] = []

    # Auth infra slot empty
    if auth_slot and getattr(auth_slot, "status", "empty") == "empty":
        out.append(
            PendingAction(
                priority="warning",
                title="No authentication deployed",
                description="Apps are publicly accessible without a login screen.",
                action="Deploy TinyAuth in Infrastructure",
                link="/infra",
                icon="\U0001f510",
            )
        )

    # No LLM model installed
    if not has_model:
        out.append(
            PendingAction(
                priority="warning",
                title="AI health monitoring inactive",
                description="No LLM model installed — health issues won't have AI-powered diagnosis.",
                action="Install Ollama and download a model (see /models)",
                link="/models",
                icon="\U0001f916",
            )
        )

    # Health check errors/warnings from recent cycle
    seen_apps: set[str] = set()
    for row in health_rows:
        if row["app_key"] not in seen_apps:
            seen_apps.add(row["app_key"])
            if row["status"] == "warning":
                out.append(
                    PendingAction(
                        priority="warning",
                        title=f"{row['app_key']} health warning",
                        description=row["summary"] or "Health check returned a warning.",
                        action="View details in Health Monitor",
                        link="/health",
                        icon="\u26a0\ufe0f",
                    )
                )

    # Wiring rows stuck pending > 30 min — configuration stalled
    for wrow in stale_wiring_rows[:5]:  # cap at 5 to avoid flooding
        age_min = int((time.time() - (wrow["wired_at"] or 0)) / 60)
        out.append(
            PendingAction(
                priority="warning",
                title=f"Wiring stalled: {wrow['source_key']} \u2192 {wrow['target_key']}",
                description=(
                    f"Wire type '{wrow['wire_type']}' has been pending for {age_min} minutes. "
                    "Both apps may need to be running and configured before wiring can complete."
                ),
                action="Check that both apps are running and their API keys are set",
                link="/health",
                icon="\U0001f517",
            )
        )

    # Probe failures with 5+ consecutive failures — scheduler probe broken
    for pfrow in probe_failure_rows[:5]:  # cap at 5
        out.append(
            PendingAction(
                priority="warning",
                title=f"Scheduler probe failing: {pfrow['probe_name']}",
                description=(
                    f"The '{pfrow['probe_name']}' probe has failed {pfrow['fail_count']} "
                    f"consecutive times. Last error: {(pfrow['last_error'] or '')[:120]}"
                ),
                action="Check server logs for probe errors; restart SLOP if the issue persists",
                link="/health",
                icon="\U0001f50d",
            )
        )
    return out


def _pending_suggestions(
    apps: list[Any], platform: Any, env_vals: dict[str, str], settings: dict[str, Any]
) -> list[PendingAction]:
    """Suggestion-tier items: no apps installed, notifications unconfigured."""
    out: list[PendingAction] = []

    # No apps installed
    running_apps = [a for a in apps if getattr(a, "status", "") == "running"]
    if not running_apps and platform.status == "ready":
        out.append(
            PendingAction(
                priority="suggestion",
                title="No apps installed yet",
                description="The platform is ready — start by installing Sonarr, Radarr and Prowlarr.",
                action="Browse the Catalog",
                link="/catalog",
                icon="\U0001f4e6",
            )
        )

    # Notifications not configured
    if not env_vals.get("NTFY_URL", "") and not (settings.get("ntfy_url") or ""):
        out.append(
            PendingAction(
                priority="suggestion",
                title="Push notifications not configured",
                description="Set up ntfy to receive alerts when apps go down or certs expire.",
                action="Configure in Settings \u2192 Notifications",
                link="/settings",
                icon="\U0001f514",
            )
        )
    return out


def compute_pending_actions() -> list[PendingAction]:
    """Return outstanding platform issues, ordered by priority.

    Sources: platform DB state, settings, installed apps, health results.
    Errors first, then warnings, then suggestions.
    """
    from backend.core.state import StateDB
    from backend.core.config import config as _cfg

    with StateDB() as db:
        platform = db.get_platform()
        settings = {
            "cf_token": db.get_setting("cf_auto_register_hostnames"),
            "ntfy_url": db.get_setting("ntfy_url"),
        }
        apps = db.get_all_apps()
        health_rows = (
            db.execute(
                "SELECT subject_key AS app_key, status, summary FROM health_checks "
                "WHERE status IN ('error','warning') AND subject_type='app' "
                "ORDER BY checked_at DESC LIMIT 50"
            ).fetchall()
            if _table_exists(db, "health_checks")
            else []
        )
        # Wiring rows stuck in 'pending' for > 30 minutes signal a configuration stall
        _wiring_stale_cutoff = int(time.time()) - 30 * 60
        stale_wiring_rows = (
            db.execute(
                "SELECT w.id, w.wire_type, w.wired_at, "
                "src.key AS source_key, tgt.key AS target_key "
                "FROM wiring w "
                "JOIN apps src ON src.id = w.source_app_id "
                "JOIN apps tgt ON tgt.id = w.target_app_id "
                "WHERE w.status = 'pending' AND w.wired_at IS NOT NULL "
                "AND w.wired_at < ?",
                (_wiring_stale_cutoff,),
            ).fetchall()
            if _table_exists(db, "wiring")
            else []
        )
        # Probe failures with 5+ consecutive failures signal a broken scheduler probe
        probe_failure_rows = (
            db.execute(
                "SELECT probe_name, fail_count, last_error, last_failed_at "
                "FROM probe_failures WHERE fail_count >= 5 "
                "ORDER BY last_failed_at DESC"
            ).fetchall()
            if _table_exists(db, "probe_failures")
            else []
        )

    env_vals = _read_env_vals(_cfg)

    with StateDB() as db:
        auth_slot = db.get_slot("auth")
    models_dir = _cfg.data_dir / "models"
    has_model = models_dir.exists() and any(models_dir.glob("*.gguf"))

    actions: list[PendingAction] = []
    actions.extend(_pending_errors(platform, apps, env_vals))
    actions.extend(
        _pending_warnings(auth_slot, has_model, health_rows, stale_wiring_rows, probe_failure_rows)
    )
    actions.extend(_pending_suggestions(apps, platform, env_vals, settings))

    # Sort: errors \u2192 warnings \u2192 suggestions
    order = {"error": 0, "warning": 1, "suggestion": 2}
    actions.sort(key=lambda a: order.get(a.priority, 3))
    return actions
