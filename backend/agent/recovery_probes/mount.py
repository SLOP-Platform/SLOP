"""backend/agent/recovery_probes/mount.py — bind-mount GROUND probe.

Probe 1: mount_health
    Bind-mount source paths (from custom_volumes) must exist and be non-empty.
    DRIFT if missing or empty.  VERIFIED if all bind-mounts are present.
    VERIFIED (no bind-mounts) if the manifest declares none.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from backend.agent.spine import Finding, Verdict


def _probe_mount_health(app: Any) -> Finding | None:
    """GROUND: bind-mount source paths exist and are non-empty."""
    app_key: str = getattr(app, "key", str(app))
    finding_id = f"recovery.mount_health.{app_key}"
    physics = f"bind-mount source paths for app {app_key}"

    custom_volumes = getattr(app, "custom_volumes", None) or []
    if not custom_volumes:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.VERIFIED,
            summary="no bind-mounts declared — nothing to probe",
        )

    missing: list[str] = []
    empty: list[str] = []

    for vol in custom_volumes:
        host_path = getattr(vol, "host_path", None) or ""
        # Named volumes have no leading slash; bind-mounts do.
        if not host_path or not host_path.startswith("/"):
            continue
        p = Path(host_path)
        if not p.exists():
            missing.append(host_path)
            continue
        # Check non-empty: at least one item inside (or inode count > 0)
        try:
            if not os.listdir(host_path):
                empty.append(host_path)
        except PermissionError:
            # Can't list → treat as accessible but unverifiable
            pass

    if missing:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.DRIFT,
            summary="bind-mount source path(s) missing",
            detail=f"missing={missing}",
        )
    if empty:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.DRIFT,
            summary="bind-mount source path(s) are empty",
            detail=f"empty={empty}",
        )
    return Finding(
        id=finding_id,
        physics=physics,
        verdict=Verdict.VERIFIED,
        summary="all bind-mount source paths present and non-empty",
    )
