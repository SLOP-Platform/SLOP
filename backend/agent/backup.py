"""backend/agent/backup.py — backup-awareness helpers (recoverability MVP).

This module is the small, shared seam between three consumers:

  * the recovery probes (``recovery_audit.py``) — surface "no backup configured"
    and (via the existing freshness probe) "backup stale";
  * the ``ms-backup`` operational script — tars an app's config volume into its
    backup directory; advisory (dry-run) by default;
  * the auto-apply path (``apply.py``) — warns, non-blocking, before a mutating
    action when no recent backup exists.

Scope is deliberately small (MVP).  It tars ONE thing — the app's per-app config
volume under ``<config_root>/<key>`` — to a local directory.  Retention, restore
verification, off-host targets, encryption and scheduling are a separate design
and are NOT implemented here.

RUNTIME-ONLY, GROUND-only: every function observes the filesystem (mtime, dir
contents) the agent already reads.  No docs, no runbooks, no network.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from backend.core.logging import get_logger

log = get_logger(__name__)

# Backups for an app live under <config_root>/<BACKUP_SUBDIR>/<key>/.  Keeping
# them under the same config_root the app data lives in means a single bind-mount
# root contains both the live data and its backups — intentional for the MVP
# (off-host targets are the deferred design).
BACKUP_SUBDIR = "backups"

# An app's latest backup is "recent" (good enough to proceed with a mutating
# action without a loud warning) when its newest artifact is younger than this.
RECENT_BACKUP_MAX_AGE_H = 24.0


def app_backup_dir(app: Any, config_root: str | None) -> str:
    """Resolve the backup directory for *app*.

    Precedence:
      1. An explicit ``backup_dir`` on the manifest (absolute path) wins — this
         is the injection seam for tests and advanced operators.
      2. Otherwise derive ``<config_root>/backups/<key>`` when a config_root is
         known.
      3. Otherwise return "" (caller treats this as "unresolvable").

    Returns a string path (possibly empty); never raises.
    """
    explicit = getattr(app, "backup_dir", None) or ""
    if explicit:
        return str(explicit)

    key = getattr(app, "key", "") or ""
    if config_root and key:
        return str(Path(config_root) / BACKUP_SUBDIR / key)
    return ""


def latest_backup_age_hours(backup_dir: str) -> float | None:
    """Age in hours of the most-recently-modified artifact in *backup_dir*.

    Returns None when the directory is absent, unreadable, or empty — i.e. when
    there is no backup to measure.  Never raises.
    """
    if not backup_dir:
        return None
    bdir = Path(backup_dir)
    try:
        if not bdir.exists():
            return None
        artifacts = [a for a in bdir.iterdir() if a.is_file()]
    except OSError as exc:
        log.debug("latest_backup_age_hours: cannot read %s: %s", backup_dir, exc)
        return None
    if not artifacts:
        return None
    latest_mtime = max(a.stat().st_mtime for a in artifacts)
    return (time.time() - latest_mtime) / 3600.0


def has_recent_backup(backup_dir: str, *, max_age_h: float = RECENT_BACKUP_MAX_AGE_H) -> bool:
    """True when *backup_dir* holds an artifact younger than *max_age_h*."""
    age = latest_backup_age_hours(backup_dir)
    return age is not None and age <= max_age_h


def verify_recent_backup_before_action(
    app: Any,
    config_root: str | None,
    *,
    action: str = "mutating action",
    max_age_h: float = RECENT_BACKUP_MAX_AGE_H,
) -> tuple[bool, str]:
    """Pre-risky-action check: is there a recent backup for *app*?

    This is intentionally NON-BLOCKING — it returns a verdict and a one-line
    reason; the caller logs a WARN and proceeds.  A backup is advisory hygiene,
    not a gate: refusing to restart a crash-looping container because it lacks a
    backup would conflate recoverability with availability.

    Returns ``(ok, reason)``:
      * ``ok=True``  — a recent backup exists, OR the app does not opt into
        backup (``backup_supported`` falsy) so there is nothing to warn about.
      * ``ok=False`` — the app supports backup but none recent exists; *reason*
        is a human one-liner suitable for a WARN log.
    """
    if not getattr(app, "backup_supported", False):
        return True, "app does not opt into backup; nothing to verify"

    backup_dir = app_backup_dir(app, config_root)
    if not backup_dir:
        return (
            False,
            "backup supported but backup directory is unresolvable "
            "(no config_root) — cannot confirm a recent backup",
        )

    age = latest_backup_age_hours(backup_dir)
    if age is None:
        return (
            False,
            f"no backup found in {backup_dir} before {action} — "
            "consider running `ms-backup <app> --execute` first",
        )
    if age > max_age_h:
        return (
            False,
            f"latest backup is {age:.0f}h old (> {max_age_h:.0f}h) before {action} — "
            "consider running `ms-backup <app> --execute` first",
        )
    return True, f"recent backup present ({age:.0f}h old)"


__all__ = [
    "BACKUP_SUBDIR",
    "RECENT_BACKUP_MAX_AGE_H",
    "app_backup_dir",
    "has_recent_backup",
    "latest_backup_age_hours",
    "verify_recent_backup_before_action",
]
