"""backend/agent/recovery_audit.py — recoverability + cert-expiry + credential probes.

Four GROUND probes reconciled against physics only:

  1. **mount_health** — bind-mount source paths (from custom_volumes) must exist
     and be non-empty.  DRIFT if missing or empty.  VERIFIED if all bind-mounts
     are present.  VERIFIED (no bind-mounts) if the manifest declares none.

  2a. **backup_configured** — soft advisory.  For an app that opts into backup
     (``backup_supported``), surface "no backup configured" (INDETERMINATE) when
     no backup directory resolves.  DISTINCT from "declared but absent" below:
     this is "you never set one up", not "the one you set up is gone".

  2b. **backup_freshness** — once a backup directory resolves (explicit
     ``backup_dir`` or ``<config_root>/backups/<key>``), it must exist and
     contain at least one artifact whose mtime is within 24h.  INDETERMINATE if
     the directory is absent (config absent ≠ failure).  DRIFT if empty or stale.

  3. **cert_expiry** — if the app manifest exposes a ``tls_cert_path`` field,
     the cert must not expire within 30 days.  DRIFT < 30 days (warn), DRIFT
     < 7 days (crit).  INDETERMINATE if the cert file is unreadable.
     Supports both PEM files and Traefik ACME JSON (auto-detected by extension).
     The ``{config_root}`` template in cert paths is resolved at reconcile time.

  4. **credential_validity** — if the app manifest declares ``auto_secrets``,
     each referenced env-var key must be present and non-empty in the platform
     ``.env`` file.  DRIFT if any declared secret is absent or empty.
     INDETERMINATE if the ``.env`` file cannot be read.

GROUND-only: no docs, no runbooks.  INDETERMINATE whenever a ground source is
unreachable; never a silent VERIFIED.

Per-domain probe implementations live in backend/agent/recovery_probes/:
  - mount.py       : _probe_mount_health
  - backup.py      : _probe_backup_configured, _probe_backup_freshness
  - cert.py        : _probe_cert_expiry
  - credential.py  : _probe_credential_validity
"""

from __future__ import annotations

from typing import Any

from backend.agent.recovery_probes import (
    _probe_backup_configured,
    _probe_backup_freshness,
    _probe_cert_expiry,
    _probe_credential_validity,
    _probe_mount_health,
)
from backend.agent.spine import Finding, Verdict
from backend.core.logging import get_logger

log = get_logger(__name__)


def reconcile_recovery(  # noqa: C901 — flat dispatch of 5 independent guarded GROUND probes (mount/backup-configured/backup-freshness/cert/credential); each carries its own INDETERMINATE fallback, splitting scatters the per-probe guard
    apps: list[Any],
    config_root: str | None = None,
    *,
    env_path: str = "",
) -> list[Finding]:
    """GROUND recoverability reconciler.

    Probes per app: mount_health + backup_configured (soft advisory) +
    backup_freshness + cert_expiry + credential_validity.  ``config_root`` (the
    platform config root) lets the backup probes resolve
    ``<config_root>/backups/<key>`` for apps that opt into backup via
    ``backup_supported``; when None, only manifests carrying an explicit
    ``backup_dir`` resolve.

    Accepts the list of installed app manifest objects.  Each probe is
    independently guarded so one failure yields its own INDETERMINATE without
    suppressing the others.  Returns all non-None findings.

    ``config_root`` is used to resolve ``{config_root}`` templates in
    ``tls_cert_path`` fields.  When empty, the reconciler tries to read it from
    the platform DB record; pass it explicitly in tests to avoid DB access.

    ``env_path`` is the path to the platform ``.env`` file for the credential
    probe.  When empty, the probe resolves it via ``backend.core.config``.
    """
    # Resolve config_root from DB when not provided explicitly. Coerce None→"" so the
    # cert probe (which takes a str) never receives None from the unified signature.
    resolved_config_root: str = config_root or ""
    if not resolved_config_root:
        try:
            from backend.core.state import StateDB

            with StateDB() as _db:
                _p = _db.get_platform()
                resolved_config_root = getattr(_p, "config_root", "") or ""
        except Exception as exc:
            log.debug("could not read config_root from DB for cert path resolution: %s", exc)

    findings: list[Finding] = []

    for app in apps:
        app_key = getattr(app, "key", "unknown")

        # Probe 2a: backup configured (soft advisory; omit if not opted in)
        try:
            f = _probe_backup_configured(app, config_root)
            if f is not None:
                findings.append(f)
        except Exception as exc:
            log.warning("backup_configured probe failed for %s: %s", app_key, exc)
            findings.append(
                Finding(
                    id=f"recovery.backup_configured.{app_key}",
                    physics=f"backup configuration for app {app_key}",
                    verdict=Verdict.INDETERMINATE,
                    summary="backup_configured probe raised unexpectedly",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

        # Probe 1: mount health
        try:
            f = _probe_mount_health(app)
            if f is not None:
                findings.append(f)
        except Exception as exc:
            log.warning("mount_health probe failed for %s: %s", app_key, exc)
            findings.append(
                Finding(
                    id=f"recovery.mount_health.{app_key}",
                    physics=f"bind-mount source paths for app {app_key}",
                    verdict=Verdict.INDETERMINATE,
                    summary="mount_health probe raised unexpectedly",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

        # Probe 2b: backup freshness (omit if no backup_dir resolvable)
        try:
            f = _probe_backup_freshness(app, config_root)
            if f is not None:
                findings.append(f)
        except Exception as exc:
            log.warning("backup_freshness probe failed for %s: %s", app_key, exc)
            findings.append(
                Finding(
                    id=f"recovery.backup_freshness.{app_key}",
                    physics=f"backup directory artifact mtime for app {app_key}",
                    verdict=Verdict.INDETERMINATE,
                    summary="backup_freshness probe raised unexpectedly",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

        # Probe 3: cert expiry (omit if no tls_cert_path)
        try:
            f = _probe_cert_expiry(app, config_root=resolved_config_root)
            if f is not None:
                findings.append(f)
        except Exception as exc:
            log.warning("cert_expiry probe failed for %s: %s", app_key, exc)
            findings.append(
                Finding(
                    id=f"recovery.cert_expiry.{app_key}",
                    physics=f"TLS certificate expiry for app {app_key}",
                    verdict=Verdict.INDETERMINATE,
                    summary="cert_expiry probe raised unexpectedly",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

        # Probe 4: credential validity (omit if no auto_secrets)
        try:
            f = _probe_credential_validity(app, env_path=env_path)
            if f is not None:
                findings.append(f)
        except Exception as exc:
            log.warning("credential_validity probe failed for %s: %s", app_key, exc)
            findings.append(
                Finding(
                    id=f"recovery.credential_validity.{app_key}",
                    physics=f"auto_secrets env-var presence + length for app {app_key}",
                    verdict=Verdict.INDETERMINATE,
                    summary="credential_validity probe raised unexpectedly",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

    return findings


__all__ = ["reconcile_recovery"]
