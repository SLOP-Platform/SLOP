"""backend/agent/recovery_probes/backup.py — backup GROUND probes.

Probe 2a: backup_configured
    Soft advisory — an app that opts into backup has a resolvable backup dir.
    DISTINCT from backup_freshness: this probe answers "is backup CONFIGURED
    at all?" not "is the existing backup recent?".

Probe 2b: backup_freshness
    GROUND: latest backup artifact not stale.  Fires once a backup_dir resolves
    to a real directory.
"""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import Any

from backend.agent.backup import app_backup_dir
from backend.agent.spine import Finding, Verdict

_BACKUP_WARN_H = 24  # hours — DRIFT (warn)
_BACKUP_CRIT_H = 72  # hours — DRIFT (crit)


def _probe_backup_configured(app: Any, config_root: str | None = None) -> Finding | None:
    """Soft advisory: an app that opts into backup has a resolvable backup dir.

    This is DISTINCT from :func:`_probe_backup_freshness`:

      * freshness answers "is the EXISTING backup recent?" and only fires once a
        ``backup_dir`` resolves to a real directory;
      * THIS probe answers "is backup CONFIGURED at all?" for an app that
        declared ``backup_supported`` — a soft INDETERMINATE advisory when the
        backup directory cannot be resolved (no config_root) so the operator
        sees "no backup configured" rather than silence.

    Returns None when the app does not opt into backup (``backup_supported``
    falsy) — no advisory for apps with no durable state to protect.
    """
    if not getattr(app, "backup_supported", False):
        return None  # app does not opt into backup — nothing to advise

    app_key: str = getattr(app, "key", str(app))
    finding_id = f"recovery.backup_configured.{app_key}"
    physics = f"backup configuration for app {app_key}"

    backup_dir = app_backup_dir(app, config_root)
    if not backup_dir:
        # Opted into backup but no directory resolves — soft advisory, not a
        # DRIFT: nothing is broken, the operator simply has not configured a
        # backup location yet.
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="no backup configured — app supports backup but none is set up",
            detail=f"app={app_key} backup_supported=true backup_dir=unresolved",
        )

    return Finding(
        id=finding_id,
        physics=physics,
        verdict=Verdict.VERIFIED,
        summary="backup directory configured",
        detail=f"path={backup_dir}",
    )


def _probe_backup_freshness(app: Any, config_root: str | None = None) -> Finding | None:
    """GROUND: latest backup artifact not stale.

    The backup directory is resolved via :func:`app_backup_dir` — an explicit
    ``backup_dir`` on the manifest, else ``<config_root>/backups/<key>`` for an
    app that opts into backup.  Returns None when no directory can be resolved
    (the "no backup configured" case is owned by ``_probe_backup_configured``).
    """
    app_key: str = getattr(app, "key", str(app))
    finding_id = f"recovery.backup_freshness.{app_key}"
    physics = f"backup directory artifact mtime for app {app_key}"

    backup_dir = app_backup_dir(app, config_root)
    if not backup_dir:
        return None  # no backup dir resolvable — freshness has nothing to probe

    bdir = Path(backup_dir)
    if not bdir.exists():
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="backup_dir declared but directory absent",
            detail=f"path={backup_dir}",
        )

    try:
        artifacts = list(bdir.iterdir())
    except PermissionError as exc:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="backup_dir unreadable",
            detail=f"PermissionError: {exc}",
        )

    if not artifacts:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.DRIFT,
            summary="backup_dir is empty — no artifacts found",
            detail=f"path={backup_dir}",
        )

    # Find the most-recently modified artifact
    latest_mtime = max(a.stat().st_mtime for a in artifacts)
    age_h = (datetime.datetime.now().timestamp() - latest_mtime) / 3600

    if age_h > _BACKUP_CRIT_H:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.DRIFT,
            summary=f"latest backup is {age_h:.0f}h old — critical",
            detail=f"path={backup_dir} age_hours={age_h:.1f} threshold_crit={_BACKUP_CRIT_H}",
        )
    if age_h > _BACKUP_WARN_H:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.DRIFT,
            summary=f"latest backup is {age_h:.0f}h old — warn",
            detail=f"path={backup_dir} age_hours={age_h:.1f} threshold_warn={_BACKUP_WARN_H}",
        )
    return Finding(
        id=finding_id,
        physics=physics,
        verdict=Verdict.VERIFIED,
        summary=f"latest backup is {age_h:.0f}h old — within threshold",
        detail=f"path={backup_dir} age_hours={age_h:.1f}",
    )
