"""backend/manifests/loader_parsers.py

Manifest YAML section parsers and validation utilities.

Extracted from loader.py (file-size discipline).
All symbols are re-exported from loader.py for backward compatibility.

Responsibilities:
  - Per-section parse functions (ports, volumes, wiring, post_deploy, health, gpu, deps)
  - Content hash helper
  - Capability allowlist and filter
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.core.logging import get_logger

if TYPE_CHECKING:
    from backend.manifests.loader import GpuDef, DependencyDef

log = get_logger(__name__)


def _require(data: dict[str, Any], key: str, path: Path) -> Any:
    from backend.manifests.loader import ManifestError

    val = data.get(key)
    if val is None or val == "":
        raise ManifestError(path, f"Missing required field: '{key}'")
    return val


def _parse_ports(data: dict[str, Any], path: Path) -> tuple[int | None, list[Any]]:
    from backend.manifests.loader import ManifestError, PortDef

    ports_raw = data.get("ports", {})
    web_port = ports_raw.get("web")
    if web_port is not None:
        try:
            web_port = int(web_port)
        except (TypeError, ValueError) as err:
            raise ManifestError(path, f"ports.web must be an integer, got: {web_port!r}") from err

    extra: list[PortDef] = []
    for p in ports_raw.get("extra", []):
        try:
            extra.append(
                PortDef(
                    internal=int(p["internal"]),
                    protocol=p.get("protocol", "tcp"),
                    name=p.get("name", ""),
                )
            )
        except (KeyError, TypeError, ValueError) as e:
            raise ManifestError(path, f"Invalid extra port definition: {e}") from e

    return web_port, extra


_PROMPT_SENTINEL_RE = None  # populated lazily below


def _extract_prompt_key(host_val: str) -> str:
    """Return the key name from a '<prompt:{key}>' sentinel, or '' if not a sentinel."""
    import re

    m = re.fullmatch(r"<prompt:([a-zA-Z_][a-zA-Z0-9_]*)>", host_val)
    return m.group(1) if m else ""


# Valid per-volume backup classes (design §14). An unknown/absent value coerces to "config" —
# the fail-safe direction (back up rather than silently exclude irreplaceable data, constraint 5).
_VOLUME_BACKUP_CLASSES = frozenset({"config", "media", "exclude"})


def _coerce_backup_class(raw: Any) -> str:
    val = str(raw or "").strip().lower()
    return val if val in _VOLUME_BACKUP_CLASSES else "config"


def _parse_volumes(data: dict[str, Any], path: Path) -> tuple[str, str | None, list[Any]]:
    from backend.manifests.loader import ManifestError, VolumeDef

    vol = data.get("volumes", {})
    config_vol = vol.get("config", "/config")
    media_vol = vol.get("media")

    custom: list[VolumeDef] = []
    for v in vol.get("custom", []):
        try:
            host_raw = v["host"]
            prompt_key = _extract_prompt_key(host_raw) if host_raw else ""
            custom.append(
                VolumeDef(
                    host_path=host_raw,
                    container_path=v["container"],
                    readonly=bool(v.get("readonly", False)),
                    prompt_key=prompt_key,
                    backup_class=_coerce_backup_class(v.get("backup_class")),
                )
            )
        except KeyError as e:
            raise ManifestError(path, f"Custom volume missing field: {e}") from e

    return config_vol, media_vol, custom


def _parse_install_prompts(data: dict[str, Any]) -> list[Any]:
    """Parse the install_prompts list from manifest YAML.

    Each entry: {key, label, description, type (path|string), required, default}.
    Returns a list of plain dicts — the frontend and API consume them directly.
    """
    raw = data.get("install_prompts") or []
    result = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        result.append(
            {
                "key": str(entry.get("key", "")),
                "label": str(entry.get("label", "")),
                "description": str(entry.get("description", "")),
                "type": str(entry.get("type", "string")),
                "required": bool(entry.get("required", False)),
                "default": str(entry.get("default", "")),
            }
        )
    return result


def _parse_wiring(data: dict[str, Any], path: Path) -> list[Any]:
    from backend.manifests.loader import ManifestError, WireDef

    wiring_raw = data.get("wiring", {})
    wires: list[WireDef] = []

    for direction in ("accepts", "connects_to"):
        peer_key = "from" if direction == "accepts" else "to"
        for entry in wiring_raw.get(direction, []):
            try:
                wires.append(
                    WireDef(
                        wire_type=entry["type"],
                        peer=entry[peer_key],
                        direction=direction,
                        description=entry.get("description", ""),
                        optional=bool(entry.get("optional", False)),
                    )
                )
            except KeyError as e:
                raise ManifestError(path, f"Wiring entry missing field: {e}") from e

    return wires


def _parse_post_deploy(data: dict[str, Any], path: Path) -> list[Any]:
    from backend.manifests.loader import ManifestError, PostDeployStep, VALID_STEP_TYPES

    steps = []
    for raw in data.get("post_deploy", []):
        step_type = raw.get("type", "")
        if step_type not in VALID_STEP_TYPES:
            raise ManifestError(
                path,
                f"Unknown post_deploy step type '{step_type}'. Valid: {sorted(VALID_STEP_TYPES)}",
            )
        steps.append(
            PostDeployStep(
                step_type=step_type,
                timeout=int(raw.get("timeout", 60)),
                path=raw.get("path", ""),
                target=raw.get("target", ""),
                wire_type=raw.get("wire_type", ""),
            )
        )
    return steps


_HEALTH_REQUIRED_TIERS: frozenset[int] = frozenset({1, 2})


def _parse_health(data: dict[str, Any], path: Path, tier: int = 0) -> tuple[list[Any], list[Any]]:
    """Parse health checks and self-heal rules from manifest data.

    ``tier`` is used to enforce that tier-1 and tier-2 manifests declare at
    least one health check.  Lower tiers (3+) are exempt.  Pass ``tier=0``
    (the default) to skip enforcement — used only by tests that exercise the
    parser in isolation without a full manifest context.
    """
    from backend.manifests.loader import (
        ManifestError,
        HealthCheckDef,
        SelfHealDef,
        VALID_CHECK_TYPES,
        VALID_HEAL_ACTIONS,
    )

    health_raw = data.get("health", {})

    checks = []
    for raw in health_raw.get("checks", []):
        check_type = raw.get("type", "http")
        if check_type not in VALID_CHECK_TYPES:
            raise ManifestError(path, f"Unknown health check type: '{check_type}'")
        checks.append(
            HealthCheckDef(
                name=raw.get("name", "default"),
                check_type=check_type,
                path=raw.get("path", ""),
                expect_status=int(raw.get("expect_status", 200)),
                interval=int(raw.get("interval", 30)),
                port=int(raw.get("port", 0)),
            )
        )

    # Tier-1 and tier-2 manifests must declare at least one health check so
    # the health scheduler has a concrete signal to drive instead of marking
    # the app running with zero verification.
    if tier in _HEALTH_REQUIRED_TIERS and not checks:
        raise ManifestError(
            path,
            f"tier-{tier} manifests must declare at least one health check "
            f"(health.checks); found none.  Add an http, tcp, process, or "
            f"custom check so the health scheduler can verify this app.",
        )

    heals = []
    for raw in health_raw.get("self_heal") or []:
        action = raw.get("action", "")
        if action not in VALID_HEAL_ACTIONS:
            raise ManifestError(path, f"Unknown self_heal action: '{action}'")
        heals.append(
            SelfHealDef(
                condition=raw.get("condition", ""),
                action=action,
                max_attempts=int(raw.get("max_attempts", 3)),
                cooldown=int(raw.get("cooldown", 60)),
            )
        )

    return checks, heals


def _parse_gpu(data: dict[str, Any]) -> GpuDef | None:
    from backend.manifests.loader import GpuDef

    gpu_raw = data.get("gpu")
    if gpu_raw is None:
        return None
    return GpuDef(
        optional=bool(gpu_raw.get("optional", True)),
        warn_if_absent=bool(gpu_raw.get("warn_if_absent", True)),
        nvidia=bool(gpu_raw.get("nvidia", True)),
        amd=bool(gpu_raw.get("amd", False)),
    )


def _parse_dependencies(data: dict[str, Any]) -> DependencyDef:
    from backend.manifests.loader import DependencyDef

    dep = data.get("dependencies", {})
    return DependencyDef(
        postgres=bool(dep.get("postgres", False)),
        redis=bool(dep.get("redis", False)),
        mariadb=bool(dep.get("mariadb", False)),
        apps=list(dep.get("apps", [])),
    )


def _content_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


# Linux capabilities that community manifests are permitted to request.
# Extend this list only after security review.
_SAFE_CAPABILITIES: frozenset[str] = frozenset(
    {
        "SYS_ADMIN",  # required for FUSE (rclone, dumb, decypharr)
        "NET_ADMIN",  # required for VPN containers
        "SYS_NICE",  # required for some media apps (e.g. plex, jellyfin)
        "DAC_OVERRIDE",  # common, low-risk file permission override
    }
)


def _filter_capabilities(raw_caps: list[str], path: Path) -> list[str]:
    """Return only capabilities in the safe allowlist; log and drop unknown ones."""
    safe = [c for c in raw_caps if c in _SAFE_CAPABILITIES]
    for _cap in set(raw_caps) - set(safe):
        log.warning("manifest %s: stripped unknown capability '%s'", path.name, _cap)
    return safe
