"""backend/platform/wizard_deploy.py

Wizard infrastructure deployment helpers and validation utilities.

Extracted from wizard.py (file-size discipline).
All symbols are re-exported from wizard.py for backward compatibility.

Responsibilities:
  - Per-slot deploy functions (tunnels, auth, VPN, dashboard, management)
  - Deploy result formatting
  - Input validation (validate_wizard)
  - Docker output cleaning helper
  - Optional socket proxy deployment
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from backend.core.logging import get_logger

if TYPE_CHECKING:
    from backend.platform.wizard import StepResult

log = get_logger(__name__)


def _try_deploy_one(
    slot: str,
    key: str,
    cfg: dict[str, Any],
    deployed: list[str],
    failed: list[str],
) -> None:
    """Deploy one infra provider. On success appends to `deployed`,
    on any error appends to `failed`. The 'none' / empty-string keys
    are no-ops so callers can pass through unselected slots."""
    if not key or key == "none":
        return
    from backend.infra.registry import get_provider as _get

    try:
        provider = _get(slot, key)
        result = provider.deploy(cfg)
        if result.ok:
            deployed.append(key)
            log.info("Wizard: deployed infra %s/%s", slot, key)
        else:
            failed.append(f"{key}: {result.detail or result.message}")
            log.warning(
                "Wizard: infra %s/%s deploy failed: %s", slot, key, result.detail or result.message
            )
    except Exception as e:
        failed.append(f"{key}: {e}")
        log.warning("Wizard: infra %s/%s exception: %s", slot, key, e)


def _deploy_tunnels(
    inp: Any, domain: str, network: str, deployed: list[str], failed: list[str]
) -> None:
    """Deploy tunnel providers (cloudflared, tailscale, headscale) —
    must be up before auth routing so Traefik can route through them."""
    for tunnel in inp.tunnels or []:
        cfg: dict[str, Any] = {"domain": domain, "network": network}
        if tunnel == "cloudflared":
            cfg["tunnel_token"] = inp.secrets.get("CF_TUNNEL_TOKEN", "") if inp.secrets else ""
        elif tunnel in ("tailscale", "headscale"):
            cfg["auth_key"] = (
                (
                    inp.secrets.get("TAILSCALE_AUTH_KEY", "")
                    or inp.secrets.get("HEADSCALE_AUTH_KEY", "")
                )
                if inp.secrets
                else ""
            )
        _try_deploy_one("tunnel", tunnel, cfg, deployed, failed)


def _deploy_auth(
    inp: Any, domain: str, network: str, deployed: list[str], failed: list[str]
) -> None:
    """Deploy the auth provider (tinyauth, authelia) — after tunnels."""
    if not inp.auth or inp.auth == "none":
        return
    cfg: dict[str, Any] = {
        "domain": domain,
        "network": network,
        "users": "",  # filled from TINYAUTH_AUTH_USERS in .env when relevant
    }
    if inp.auth == "tinyauth" and inp.secrets:
        cfg["users"] = inp.secrets.get("TINYAUTH_AUTH_USERS", "")
    _try_deploy_one("auth", inp.auth, cfg, deployed, failed)


def _deploy_dashboard(
    inp: Any, domain: str, network: str, deployed: list[str], failed: list[str]
) -> None:
    """Deploy the dashboard provider (glance, homepage)."""
    if not inp.dashboard or inp.dashboard == "none":
        return
    _try_deploy_one(
        "dashboard",
        inp.dashboard,
        {"domain": domain, "network": network},
        deployed,
        failed,
    )


def _deploy_management(
    inp: Any, domain: str, network: str, deployed: list[str], failed: list[str]
) -> None:
    """Deploy the container-management provider (dockge, portainer, dockhand, komodo)."""
    if not inp.management or inp.management == "none":
        return
    cfg: dict[str, Any] = {"domain": domain, "network": network}
    if inp.management == "komodo" and inp.secrets:
        cfg["jwt_secret"] = inp.secrets.get("KOMODO_JWT_SECRET", "")
        cfg["passkey"] = inp.secrets.get("KOMODO_PASSKEY", "")
    _try_deploy_one("management", inp.management, cfg, deployed, failed)


def _format_deploy_result(deployed: list[str], skipped: list[str], failed: list[str]) -> StepResult:
    """Render the deploy_infra step's StepResult from the three buckets.

    - All three empty → 'skipped' (no providers selected)
    - Only failures   → 'error'
    - Otherwise       → 'ok' (success or partial-success; failures listed)
    """
    from backend.platform.wizard import StepResult

    parts = []
    if deployed:
        parts.append(f"Deployed: {', '.join(deployed)}")
    if skipped:
        parts.append(f"Skipped: {', '.join(skipped)}")
    if failed:
        parts.append(f"Failed: {', '.join(failed)}")
    if not deployed and not failed:
        return StepResult(
            "deploy_infra",
            "skipped",
            "No infrastructure providers selected.",
            "",
        )
    if failed and not deployed:
        return StepResult(
            "deploy_infra",
            "error",
            "Infrastructure deployment failed.",
            "\n".join(failed),
        )
    return StepResult(
        "deploy_infra",
        "ok",
        " · ".join(parts),
        "\n".join(failed) if failed else "",
    )


def validate_wizard(inp: Any) -> list[dict[str, str]]:
    """Validate wizard input before running.

    Returns a list of {field, message} dicts for any problems found.
    An empty list means the input is valid.
    """
    issues = []

    if not inp.domain or "." not in inp.domain:
        issues.append(
            {
                "field": "domain",
                "message": "Domain must be a valid hostname like 'example.com'.",
            }
        )

    if not inp.config_root or not inp.config_root.startswith("/"):
        issues.append(
            {
                "field": "config_root",
                "message": "Config root must be an absolute path starting with '/'.",
            }
        )

    if not inp.media_root or not inp.media_root.startswith("/"):
        issues.append(
            {
                "field": "media_root",
                "message": "Media root must be an absolute path starting with '/'.",
            }
        )

    if inp.puid < 0 or inp.pgid < 0:
        issues.append(
            {
                "field": "puid",
                "message": "PUID and PGID must be non-negative integers.",
            }
        )

    # Timezone validation — must be a valid IANA zone name
    if not inp.timezone:
        issues.append(
            {
                "field": "timezone",
                "message": "Timezone is required. Example: 'America/Los_Angeles'.",
            }
        )
    else:
        try:
            from zoneinfo import available_timezones

            if inp.timezone not in available_timezones():
                issues.append(
                    {
                        "field": "timezone",
                        "message": (
                            "'" + inp.timezone + "' is not a valid IANA timezone name. "
                            "Example: 'America/Los_Angeles'. "
                            "Use the timezone dropdown to pick a valid zone."
                        ),
                    }
                )
        except Exception:  # noqa: S110  # zoneinfo unavailable in some environments — skip timezone validation
            pass

    return issues


def _clean_docker_output(raw: str) -> str:
    """Summarise Docker output — strip progress bars, keep error lines."""
    lines = []
    for line in raw.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Skip pull progress lines
        if any(
            stripped.startswith(p)
            for p in (
                "Pulling",
                "Waiting",
                "Downloading",
                "Extracting",
                "Pull complete",
                "Already exists",
                "Digest:",
                "Status:",
            )
        ):
            continue
        lines.append(stripped)
    return "\n".join(lines) if lines else raw[:500]
