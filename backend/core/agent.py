"""backend/core/agent.py

SLOP Agent is the SLOP application core executive manager — its primary
responsibility is ensuring SLOP itself is running and healthy; it monitors all
managed components, detects failures, and drives automated remediation.
It is NOT merely a diagnostic add-on.

This module owns the canonical constants for the SLOP Agent and provides:
  ensure_agent_registered()  — startup hook; idempotent DB bootstrap
  check_agent_connectivity() — async probe of the configured LLM backend;
                               writes live status to health_checks each cycle
  _write_agent_health()      — sync helper to persist a status string + summary

The SLOP Agent is NOT a Docker-based catalog app.  It is the backend process
itself acting as the executive manager: authoritative monitor and remediator
for every component SLOP owns.  Its DB record (tier=0, category="agent") is
the anchor for health checks, operations, and remediation storage.

Tier meanings:
  0  — system component (SLOP Agent, future core services)
  1  — (reserved)
  2  — standard catalog app (default)
  3  — community / custom-installed app
"""

from __future__ import annotations

import json as _json
from typing import Any

from backend.core.logging import get_logger
from backend.core.state import StateDB
from backend.platform.ollama_runtime import normalize_llm_agent_config

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

AGENT_KEY: str = "slop_agent"
AGENT_DISPLAY_NAME: str = "SLOP Agent — Executive Manager"
AGENT_ROLE: str = "executive_manager"
AGENT_TIER: int = 0
AGENT_CATEGORY: str = "agent"
AGENT_SUBJECT_TYPE: str = "agent"

# Process-integrity dimension — tracked alongside LLM connectivity so SLOP's
# own rule-enforcement coverage is observable as a first-class health signal.
AGENT_SUBJECT_TYPE_INTEGRITY: str = "process_integrity"
AGENT_INTEGRITY_KEY: str = "enforcement_coverage"

HEALTH_CHECK_AGENT_STATUS: str = "agent_status"


def get_reality_view() -> dict[str, Any]:
    """Return the SLOP Agent's RealityView of the running instance (GROUND data).

    Thin re-export of ``backend.core.reality_view.assemble_live_reality_view``
    so the agent module stays the single entry point for the executive-manager
    surface while the RealityView assembly lives in its own module (file-size
    discipline).  Runtime-only: observes the live process / OS / filesystem and
    never reads docs.
    """
    from backend.core.reality_view import assemble_live_reality_view

    return assemble_live_reality_view()


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def ensure_agent_registered() -> None:
    """Idempotent startup hook.

    Guarantees that:
    1. The slop_agent app record exists in the DB with tier=0, category="agent".
       If the record already exists, only display_name and manifest_source are
       refreshed — status, tier, and category are never overwritten on restart.
    2. A baseline health check row (subject_type="agent", status="unknown")
       exists so health queries always return a result.

    Safe to call multiple times — every operation is a no-op if the data is
    already correct.
    """
    with StateDB() as db:
        existing = db.get_app(AGENT_KEY)
        if existing is None:
            db.upsert_app(
                AGENT_KEY,
                display_name=AGENT_DISPLAY_NAME,
                tier=AGENT_TIER,
                category=AGENT_CATEGORY,
                status="running",  # backend up = agent running; Phase B refines this
                image="",
                image_tag="",
                container_name=AGENT_KEY,
                manifest_source="system",
            )
            log.info("SLOP Agent record created (tier=0)")
        else:
            # Refresh human-readable fields only; never touch status/tier/category
            db.upsert_app(
                AGENT_KEY,
                display_name=AGENT_DISPLAY_NAME,
                manifest_source="system",
            )

        # Register baseline health check if none exists yet.
        # Phase B will populate this with real connectivity status.
        existing_checks = db.get_health_checks(
            subject_type=AGENT_SUBJECT_TYPE,
            subject_key=AGENT_KEY,
        )
        if not existing_checks:
            db.upsert_health_check(
                subject_type=AGENT_SUBJECT_TYPE,
                subject_key=AGENT_KEY,
                check_name=HEALTH_CHECK_AGENT_STATUS,
                status="unknown",
                summary="SLOP Agent registered — health check pending first cycle",
            )
            log.info("SLOP Agent baseline health check registered")


# ---------------------------------------------------------------------------
# Phase B — Live connectivity probe (called every health cycle)
# ---------------------------------------------------------------------------

# Provider sets for routing logic
_LOCAL_OAI_PROVIDERS: frozenset[str] = frozenset({"shimmy", "localai"})
_CLOUD_PROVIDERS: frozenset[str] = frozenset(
    {
        "groq",
        "cerebras",
        "openrouter",
        "mistral",
        "cohere",
        "google",
        "anthropic",
        "openai",
        "opencode",
        "neuralwatt",
        "nanogpt",
        "cline_pass",
        "commandcode",
        "minimax",
        "chutes",
        "trae",
        "deepinfra",
        "synthetic_new",
        "nim",
        "gai",
    }
)


def _write_agent_health(
    status: str,
    summary: str,
    detail: str | None = None,
) -> None:
    """Persist an agent health check result to DB.  Never raises."""
    try:
        with StateDB() as db:
            db.upsert_health_check(
                subject_type=AGENT_SUBJECT_TYPE,
                subject_key=AGENT_KEY,
                check_name=HEALTH_CHECK_AGENT_STATUS,
                status=status,
                summary=summary,
                detail=detail,
            )
        log.debug("Agent health written: %s — %s", status, summary)
    except Exception as exc:
        log.warning("Failed to write agent health check: %s", exc)


async def check_agent_connectivity() -> str:
    """Probe the configured LLM backend and update the health_checks DB record.

    Called by run_health_cycle() on every health check pass.  Never raises —
    all exceptions are caught and recorded as 'error'.

    Status values written to DB:
      running  — backend probe succeeded (or cloud provider has an API key)
      error    — backend unreachable, misconfigured, or key missing
      disabled — LLM agent explicitly disabled by the user

    Returns the status string.
    """
    import httpx as _httpx

    # Load config from DB (graceful on missing / malformed)
    try:
        with StateDB() as _db:
            _raw = _db.get_setting("llm_agent_config")
        cfg: dict[str, Any] = normalize_llm_agent_config(_json.loads(_raw) if _raw else {})
    except Exception:
        cfg = {}

    enabled: bool = cfg.get("enabled", True)
    provider: str = (cfg.get("provider", "") or "ollama").strip()

    # ── Disabled ────────────────────────────────────────────────────────────
    if not enabled:
        _write_agent_health(
            "disabled",
            "LLM agent disabled — configure a provider in Settings → AI / LLM.",
        )
        return "disabled"

    # ── Cloud providers — API key presence is the connectivity signal ────────
    if provider in _CLOUD_PROVIDERS:
        api_key: str = (cfg.get("api_key", "") or "").strip()
        if api_key:
            _write_agent_health(
                "running",
                f"Cloud provider '{provider}' configured with API key.",
            )
            return "running"
        _write_agent_health(
            "error",
            f"Cloud provider '{provider}' selected but no API key configured. "
            "Add one in Settings → AI / LLM.",
        )
        return "error"

    # ── Local providers — HTTP probe ─────────────────────────────────────────
    if provider == "ollama":
        base_url: str = (cfg.get("ollama_url", "") or "http://localhost:11434").strip()
        probe_path = "/api/tags"
    elif provider == "llamacpp":
        base_url = (cfg.get("llamacpp_url", "") or "http://localhost:8081").strip()
        probe_path = "/v1/models"
    else:
        # shimmy, localai, and any unknown local provider
        base_url = (cfg.get("ollama_url", "") or "http://localhost:8081").strip()
        probe_path = "/v1/models"

    api_key = (cfg.get("api_key", "") or "").strip()
    headers: dict[str, str] = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    from backend.core.url_guard import UrlNotAllowed, assert_not_metadata_url
    from backend.core.url_guard_httpx import pinned_async_client

    try:
        # SSRF floor (#1193): ollama_url/llamacpp_url are operator-settable, and this
        # probe runs unattended on every health cycle (checker.py) — refuse a
        # cloud-metadata/link-local target before the auto-fetch reaches it. The pinned
        # client also closes the connect-time DNS-rebind TOCTOU (layer-2, #1193).
        assert_not_metadata_url(base_url, resolve_dns=False)
        async with pinned_async_client(timeout=5.0) as client:
            r = await client.get(f"{base_url}{probe_path}", headers=headers)
        if r.status_code == 200:
            _write_agent_health(
                "running",
                f"{provider.capitalize()} reachable at {base_url}.",
            )
            return "running"
        _write_agent_health(
            "error",
            f"{provider.capitalize()} returned HTTP {r.status_code} at {base_url}.",
            detail=r.text[:200] if r.text else None,
        )
        return "error"

    except UrlNotAllowed:
        _write_agent_health(
            "error",
            f"Refusing to probe {provider}: {base_url} targets a cloud-metadata/link-local "
            "address (SSRF floor). Point ollama_url/llamacpp_url at a real LLM endpoint.",
        )
        return "error"
    except _httpx.ConnectError:
        _write_agent_health(
            "error",
            f"Cannot reach {provider} at {base_url} — check that the service is running.",
        )
        return "error"
    except _httpx.TimeoutException:
        _write_agent_health(
            "error",
            f"Connection to {provider} at {base_url} timed out (5s).",
        )
        return "error"
    except Exception as exc:
        _write_agent_health(
            "error",
            f"Unexpected error probing {provider}: {str(exc)[:100]}",
        )
        return "error"
