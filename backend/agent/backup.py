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

import json
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

# The platform DB's own backups live under <config_root>/<BACKUP_SUBDIR>/_platform/ (written by
# `ms-backup --platform`). The leading underscore keeps it out of the per-app key namespace.
# DUPLICATED from backend.platform.backup_ops.PLATFORM_BACKUP_SUBDIR on purpose: the agent read
# tier never imports the operational tier (the observe-only firewall — same reason _VERIFY_SIDECAR_*
# is mirrored here). Equality is pinned by tests/test_backup_ops.py::test_platform_subdir_contract,
# so the two can never drift to different directories (#1281).
PLATFORM_BACKUP_SUBDIR = "_platform"

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


def platform_backup_dir(config_root: str | None) -> str:
    """Resolve the platform DB's backup directory: ``<config_root>/backups/_platform``.

    The system-level analogue of :func:`app_backup_dir` — there is no per-app key, the subdir is
    the fixed ``_platform``. Returns ``""`` when *config_root* is unknown (caller treats that as
    "unresolvable" and stays silent). Never raises.
    """
    return str(Path(config_root) / BACKUP_SUBDIR / PLATFORM_BACKUP_SUBDIR) if config_root else ""


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
        # Count BACKUP ARTIFACTS only — exclude .verify-*.json sidecars and .tmp partials. A dir
        # holding only a sidecar (its artifact lost/pruned) has no backup to measure; counting the
        # sidecar's mtime would falsely report a recent backup (silent-green caught in review).
        artifacts = [
            a
            for a in bdir.iterdir()
            if a.is_file()
            and not a.name.startswith(_VERIFY_SIDECAR_PREFIX)
            and not a.name.endswith(".tmp")
        ]
    except OSError as exc:
        log.debug("latest_backup_age_hours: cannot read %s: %s", backup_dir, exc)
        return None
    if not artifacts:
        return None
    latest_mtime = max(a.stat().st_mtime for a in artifacts)
    return (time.time() - latest_mtime) / 3600.0


# Restore-verify sidecars are WRITTEN by backend.platform.backup_ops (the operational tier);
# the agent only READS them — extraction is an action, reading a result is an observation
# (two-owner firewall, CLAUDE.md "Knowledge-Lifecycle").  The prefix+suffix mirror
# backup_ops.VERIFY_SIDECAR_{PREFIX,SUFFIX}; the agent must NOT import the operational layer, so
# the contract is pinned equal by tests/test_backup_ops.py::test_sidecar_prefix_contract instead.
_VERIFY_SIDECAR_PREFIX = ".verify-"
_VERIFY_SIDECAR_SUFFIX = ".json"


def latest_verify_result(backup_dir: str) -> tuple[str | None, float | None]:
    """``(verdict, age_hours)`` of the newest VALID restore-verify sidecar in *backup_dir*.

    GROUND, observe-only: reads the sidecar files ``backup_ops`` wrote (``{result, ts}``); it
    never runs a restore.  This is what makes the recovery-audit "last-restore-verify=DRIFT"
    red-signal real instead of a self-asserted log line (docs/BACKUP-PRODUCT-868-DESIGN.md §4/§9).

    Robustness (review fix): only ``.verify-*.json`` files count — a half-written
    ``.verify-*.json.tmp`` (a crash mid atomic-write) is ignored, never parsed.  Sidecars are
    walked newest-first and the first VALID one wins, so a single malformed/partial newest sidecar
    does NOT hide an older real verdict.  Returns ``(None, None)`` only when NO valid sidecar
    exists — the agent treats that as "unverified", NEVER as verified (no silent green).  Never raises.
    """
    payload, age_h = _latest_valid_sidecar(backup_dir)
    if payload is None:
        return None, None
    return str(payload["result"]), age_h


# verify_scope default for a sidecar that predates the design §13 field — conservative
# (RED-for-offhost): a missing scope is treated as local-plaintext, NEVER as offhost-verified.
_DEFAULT_VERIFY_SCOPE = "local-plaintext"


def latest_verify_scope(backup_dir: str) -> str | None:
    """The ``verify_scope`` of the newest VALID restore-verify sidecar, or ``None`` if none exists.

    GROUND, observe-only (design §13 SIDECAR CONTRACT). A sidecar written before the scope field
    existed lacks it ⇒ this returns :data:`_DEFAULT_VERIFY_SCOPE` (``local-plaintext``) — the
    conservative default so an off-host-configured app is never silently treated as off-host-verified
    by an old local-only sidecar. ``None`` only when NO valid sidecar exists (unverified)."""
    payload, _ = _latest_valid_sidecar(backup_dir)
    if payload is None:
        return None
    scope = payload.get("verify_scope")
    return str(scope) if scope else _DEFAULT_VERIFY_SCOPE


def _latest_valid_sidecar(backup_dir: str) -> tuple[dict[str, Any] | None, float | None]:
    """``(payload, age_hours)`` of the newest VALID ``.verify-*.json`` sidecar, or ``(None, None)``.

    Shared walk for :func:`latest_verify_result` + :func:`latest_verify_scope`: sidecars are read
    newest-first and the first one that parses as JSON with a ``result`` key wins, so a single
    malformed/partial newest sidecar does NOT hide an older real verdict (``.tmp`` partial-writes
    are excluded by the suffix filter). Never raises."""
    if not backup_dir:
        return None, None
    bdir = Path(backup_dir)
    try:
        if not bdir.exists():
            return None, None
        sidecars = [
            s
            for s in bdir.iterdir()
            if s.is_file()
            and s.name.startswith(_VERIFY_SIDECAR_PREFIX)
            and s.name.endswith(_VERIFY_SIDECAR_SUFFIX)  # excludes the .tmp partial-write
        ]
    except OSError as exc:
        log.debug("_latest_valid_sidecar: cannot read %s: %s", backup_dir, exc)
        return None, None

    for sidecar in sorted(sidecars, key=lambda s: s.stat().st_mtime, reverse=True):
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            payload["result"]  # KeyError if absent → skip as malformed
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.debug("_latest_valid_sidecar: skipping malformed sidecar %s: %s", sidecar, exc)
            continue
        age_h = (time.time() - sidecar.stat().st_mtime) / 3600.0
        return payload, age_h
    return None, None


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
    "PLATFORM_BACKUP_SUBDIR",
    "RECENT_BACKUP_MAX_AGE_H",
    "app_backup_dir",
    "latest_backup_age_hours",
    "latest_verify_result",
    "latest_verify_scope",
    "platform_backup_dir",
    "verify_recent_backup_before_action",
]
