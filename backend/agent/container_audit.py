"""backend/agent/container_audit.py — container substrate probes.

Two GROUND probes that observe Docker container state and emit frozen-verdict
:class:`Finding` objects.  No remediation is wired — alert-only.

Probes:
  1. restart_loop     — detect managed containers with excessive restart counts
  2. orphaned         — detect managed containers with no matching installed app

INDETERMINATE when Docker is unavailable or subprocess fails; never a silent VERIFIED.
Each probe is independently guarded.
Detail fields contain no hostnames, usernames, or IP addresses.
"""

from __future__ import annotations

import re
import subprocess

from backend.agent.spine import Finding, Verdict
from backend.core.logging import get_logger
from backend.manifests.loader import load_all_manifests

log = get_logger(__name__)

_MANAGED_CONTAINER_RE = re.compile(r"^/?slop-(.+?)-\d+$")

_PHYSICS_RESTART = "docker ps -q + docker inspect RestartCount"
_PHYSICS_ORPHANED = "docker ps --filter name=slop + manifest registry"

_RESTART_THRESHOLD = 5


def _extract_app_key(container_name: str) -> str | None:
    """Return app_key from a container name, or None if not a managed container."""
    m = _MANAGED_CONTAINER_RE.match(container_name)
    return m.group(1) if m else None


def _restart_indeterminate(summary: str) -> Finding:
    """Build an INDETERMINATE Finding for the restart-loop probe."""
    return Finding(
        id="container.restart_loop",
        physics=_PHYSICS_RESTART,
        verdict=Verdict.INDETERMINATE,
        summary=summary,
        detail="",
    )


def _list_running_container_ids() -> tuple[list[str] | None, Finding | None]:
    """Return (container_ids, None) on success, or (None, Finding) on failure."""
    try:
        ps_result = subprocess.run(
            ["docker", "ps", "-q"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except FileNotFoundError:
        return None, _restart_indeterminate("restart-loop probe: docker not found")
    except subprocess.TimeoutExpired:
        return None, _restart_indeterminate("restart-loop probe: docker ps timed out")
    except subprocess.CalledProcessError as exc:
        return None, _restart_indeterminate(
            f"restart-loop probe: docker ps failed (rc={exc.returncode})"
        )
    container_ids = [line.strip() for line in ps_result.stdout.splitlines() if line.strip()]
    return container_ids, None


def _parse_inspect_line(line: str) -> tuple[str, int] | None:
    """Parse a 'name restart_count' inspect line into a managed-container tuple.

    Returns None when the line is empty, malformed, or not a managed container.
    """
    line = line.strip()
    if not line:
        return None
    parts = line.split()
    if len(parts) < 2:
        return None
    name = parts[0]
    try:
        restart_count = int(parts[1])
    except ValueError:
        return None
    if _extract_app_key(name) is None:
        return None
    return name, restart_count


def _inspect_managed_containers(
    container_ids: list[str],
) -> tuple[list[tuple[str, int]] | None, Finding | None]:
    """Inspect each container; return (managed, None) or (None, Finding) on failure."""
    managed: list[tuple[str, int]] = []  # (name, restart_count)
    for cid in container_ids:
        try:
            inspect_result = subprocess.run(
                ["docker", "inspect", "--format", "{{.Name}} {{.RestartCount}}", cid],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except FileNotFoundError:
            return None, _restart_indeterminate(
                "restart-loop probe: docker not found during inspect"
            )
        except subprocess.TimeoutExpired:
            return None, _restart_indeterminate("restart-loop probe: docker inspect timed out")
        except subprocess.CalledProcessError as exc:
            return None, _restart_indeterminate(
                f"restart-loop probe: docker inspect failed (rc={exc.returncode})"
            )
        parsed = _parse_inspect_line(inspect_result.stdout)
        if parsed is not None:
            managed.append(parsed)
    return managed, None


def _restart_verdict(managed: list[tuple[str, int]]) -> Finding:
    """Build the final DRIFT/VERIFIED Finding from inspected managed containers."""
    looping = [(name, rc) for name, rc in managed if rc >= _RESTART_THRESHOLD]
    if looping:
        worst_name, worst_rc = max(looping, key=lambda x: x[1])
        # Build detail: all containers with restarts >= 1, sorted descending
        restarted = sorted(
            [(name, rc) for name, rc in managed if rc >= 1],
            key=lambda x: x[1],
            reverse=True,
        )
        detail_lines = [f"{name} restarts={rc}" for name, rc in restarted]
        return Finding(
            id="container.restart_loop",
            physics=_PHYSICS_RESTART,
            verdict=Verdict.DRIFT,
            summary=f"container restart-loop detected: {worst_name} restarted {worst_rc} times",
            detail="\n".join(detail_lines),
        )
    return Finding(
        id="container.restart_loop",
        physics=_PHYSICS_RESTART,
        verdict=Verdict.VERIFIED,
        summary=f"container restart-loop ok: {len(managed)} managed container(s) healthy",
        detail="",
    )


def _probe_restart_loop() -> Finding:
    """GROUND source 1: detect managed containers with RestartCount >= 5."""
    container_ids, failure = _list_running_container_ids()
    if failure is not None:
        return failure
    if not container_ids:
        return _restart_indeterminate("restart-loop probe: no running containers")

    managed, failure = _inspect_managed_containers(container_ids)
    if failure is not None:
        return failure
    if not managed:
        return _restart_indeterminate("restart-loop probe: no managed containers found")

    return _restart_verdict(managed)


def _probe_orphaned_containers() -> Finding:
    """GROUND source 2: detect managed containers with no matching installed app."""
    # Step 1: list managed containers
    try:
        ps_result = subprocess.run(
            ["docker", "ps", "--filter", "name=slop", "--format", "{{.Names}}"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        container_names = [line.strip() for line in ps_result.stdout.splitlines() if line.strip()]
    except FileNotFoundError:
        return Finding(
            id="container.orphaned",
            physics=_PHYSICS_ORPHANED,
            verdict=Verdict.INDETERMINATE,
            summary="orphaned probe: docker not found",
            detail="",
        )
    except subprocess.TimeoutExpired:
        return Finding(
            id="container.orphaned",
            physics=_PHYSICS_ORPHANED,
            verdict=Verdict.INDETERMINATE,
            summary="orphaned probe: docker ps timed out",
            detail="",
        )
    except subprocess.CalledProcessError as exc:
        return Finding(
            id="container.orphaned",
            physics=_PHYSICS_ORPHANED,
            verdict=Verdict.INDETERMINATE,
            summary=f"orphaned probe: docker ps failed (rc={exc.returncode})",
            detail="",
        )

    # Step 2: load installed app keys
    try:
        manifests = load_all_manifests()
        installed_keys = set(manifests.keys())
    except Exception as exc:
        return Finding(
            id="container.orphaned",
            physics=_PHYSICS_ORPHANED,
            verdict=Verdict.INDETERMINATE,
            summary=f"orphaned probe: manifest load failed: {type(exc).__name__}",
            detail="",
        )

    # Step 3: identify orphaned containers
    orphaned: list[str] = []
    for name in container_names:
        app_key = _extract_app_key(name)
        if app_key is not None and app_key not in installed_keys:
            orphaned.append(name)

    if orphaned:
        return Finding(
            id="container.orphaned",
            physics=_PHYSICS_ORPHANED,
            verdict=Verdict.DRIFT,
            summary=f"orphaned containers found: {len(orphaned)} container(s) have no installed app",
            detail="\n".join(sorted(orphaned)),
        )

    return Finding(
        id="container.orphaned",
        physics=_PHYSICS_ORPHANED,
        verdict=Verdict.VERIFIED,
        summary="orphaned containers ok: all managed containers have a matching installed app",
        detail="",
    )


def reconcile_containers() -> list[Finding]:
    """The container substrate GROUND reconciler.

    Returns one :class:`Finding` per probe.  Each probe is independently
    guarded so one unreachable source yields its own INDETERMINATE without
    suppressing the others.  Reads no docs.
    """
    probes = [
        ("container.restart_loop", _probe_restart_loop),
        ("container.orphaned", _probe_orphaned_containers),
    ]
    findings: list[Finding] = []
    for finding_id, probe_fn in probes:
        try:
            findings.append(probe_fn())
        except Exception as exc:
            log.warning("container_audit probe %s failed unexpectedly: %s", finding_id, exc)
            findings.append(
                Finding(
                    id=finding_id,
                    physics="container substrate probe",
                    verdict=Verdict.INDETERMINATE,
                    summary=f"probe raised unexpected exception: {type(exc).__name__}",
                    detail="",
                )
            )
    return findings


__all__ = ["reconcile_containers"]
