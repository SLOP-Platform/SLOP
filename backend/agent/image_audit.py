"""backend/agent/image_audit.py — image drift probe.

Detects when a running container's image reference diverges from the
configured app manifest image.  Alert-only; no remediation.

GROUND source: ``docker inspect <container_name> --format {{.Config.Image}}``

Probe:
  _probe_image_drift(app) — compare running image to manifest expected image.
  Returns DRIFT if they differ, VERIFIED if they match, INDETERMINATE if
  Docker is unavailable, times out, or the container is not found.

Detail fields contain image references only; no hostnames, IPs, or usernames.
"""

from __future__ import annotations

import subprocess
from typing import Any

from backend.agent.spine import Finding, Verdict
from backend.core.logging import get_logger

log = get_logger(__name__)


def _normalize_image_ref(ref: str) -> str:
    """Strip well-known registry and library/ prefixes for comparison."""
    ref = ref.removeprefix("docker.io/library/")
    ref = ref.removeprefix("docker.io/")
    ref = ref.removeprefix("library/")
    return ref


def _probe_image_drift(app: Any) -> Finding | None:
    """Probe one app for image drift.

    Returns a :class:`Finding` if the app has a ``container_name``; returns
    ``None`` if the app has no ``container_name`` (probe is skipped).
    """
    container_name = getattr(app, "container_name", None)
    if container_name is None:
        return None

    image = getattr(app, "image", "")
    image_tag = getattr(app, "image_tag", "")
    app_key = getattr(app, "app_key", container_name)
    expected_image = f"{image}:{image_tag}"
    physics = f"docker inspect {container_name} .Config.Image"
    finding_id = f"image.drift.{app_key}"

    try:
        result = subprocess.run(
            ["docker", "inspect", container_name, "--format", "{{.Config.Image}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except FileNotFoundError:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary=f"image drift probe: docker not found for {app_key}",
            detail="",
        )
    except subprocess.TimeoutExpired:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary=f"image drift probe: docker inspect timed out for {app_key}",
            detail="",
        )

    if result.returncode != 0:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary=f"image drift probe: container not found for {app_key}",
            detail="",
        )

    running_image = result.stdout.strip()

    if _normalize_image_ref(running_image) != _normalize_image_ref(expected_image):
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.DRIFT,
            summary=f"image drift: {app_key} running {running_image}, expected {expected_image}",
            detail=f"running={running_image} expected={expected_image}",
        )

    return Finding(
        id=finding_id,
        physics=physics,
        verdict=Verdict.VERIFIED,
        summary=f"image ok: {app_key} running expected image",
        detail=f"image={running_image}",
    )


def reconcile_images(apps: list[Any]) -> list[Finding]:
    """The image drift GROUND reconciler.

    Iterates over all apps, calling :func:`_probe_image_drift` for each.
    Apps without a ``container_name`` are skipped (probe returns ``None``).
    Each probe call is independently guarded so one failure does not suppress
    the others.  Returns an empty list if no apps have a ``container_name``.
    """
    findings: list[Finding] = []
    for app in apps:
        app_key = getattr(app, "app_key", repr(app))
        try:
            result = _probe_image_drift(app)
            if result is not None:
                findings.append(result)
        except Exception as exc:
            log.warning("image_audit probe %s failed unexpectedly: %s", app_key, exc)
            container_name = getattr(app, "container_name", None) or app_key
            findings.append(
                Finding(
                    id=f"image.drift.{app_key}",
                    physics=f"docker inspect {container_name} .Config.Image",
                    verdict=Verdict.INDETERMINATE,
                    summary=f"probe raised unexpected exception: {type(exc).__name__}",
                    detail="",
                )
            )
    return findings


__all__ = ["reconcile_images"]
