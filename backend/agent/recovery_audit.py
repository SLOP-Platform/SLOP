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

  2c. **backup_verify** — the recoverability KEYSTONE (#868 §9): surfaces the
     restore-verify verdict recorded by ``backup_ops.verify_backup_artifact`` (read
     observe-only via ``backup.latest_verify_result``).  DRIFT when the latest backup
     FAILED to restore (freshness alone cannot catch this).  Silent until a verify
     has run (no sidecar → None), so it adds no noise before the P1b verify wiring.

  2d. **backup_schedule_overdue** — system-level (#868 P3 §9), runs once outside the per-app
     loop: GROUND on ``systemctl is-active ms-backup.timer``.  DRIFT when the scheduled-backup
     timer is inactive/failed (the scheduler stopped — a recoverability gap BEFORE backups go
     stale).  INDETERMINATE on a non-systemd host.  Observe-only (never starts the timer).

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
    _probe_backup_schedule_overdue,
    _probe_backup_verify_result,
    _probe_cert_expiry,
    _probe_custom_volume_verify_results,
    _probe_credential_validity,
    _probe_media_volume_index_declared,
    _probe_mount_health,
    _probe_platform_backup_verify_result,
)
from backend.agent.spine import Finding, Verdict
from backend.core.logging import get_logger

log = get_logger(__name__)


def reconcile_recovery(  # noqa: C901 — flat dispatch of 6 per-app guarded GROUND probes (mount/backup-configured/backup-freshness/backup-verify/cert/credential) + 2 system-level (backup-schedule-overdue #868 P3, platform-backup-verify #1281, run once after the loop); each carries its own INDETERMINATE fallback, splitting scatters the per-probe guard
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

        # Probe 2c: restore-verify result (omit if no backup_dir or no verify has run)
        try:
            f = _probe_backup_verify_result(app, config_root)
            if f is not None:
                findings.append(f)
        except Exception as exc:
            log.warning("backup_verify probe failed for %s: %s", app_key, exc)
            findings.append(
                Finding(
                    id=f"recovery.backup_verify.{app_key}",
                    physics=f"restore-verify result for app {app_key}",
                    verdict=Verdict.INDETERMINATE,
                    summary="backup_verify probe raised unexpectedly",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

        # Probe 2e: media-class volume must declare its index (design §14; omit if no media volume)
        try:
            f = _probe_media_volume_index_declared(app)
            if f is not None:
                findings.append(f)
        except Exception as exc:
            log.warning("media_index probe failed for %s: %s", app_key, exc)
            findings.append(
                Finding(
                    id=f"recovery.media_index_declared.{app_key}",
                    physics=f"media-class volume index declaration for app {app_key}",
                    verdict=Verdict.INDETERMINATE,
                    summary="media_index_declared probe raised unexpectedly",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

        # Probe 2f: per-volume restore-verify for config-class custom volumes (#1292; #1281-class).
        # Returns a LIST (one finding per volume subdir carrying a verdict) — extend, don't append.
        # Empty until an app declares custom_volumes AND a verify sidecar exists (no-noise contract).
        try:
            findings.extend(_probe_custom_volume_verify_results(app, config_root))
        except Exception as exc:
            log.warning("custom_volume_verify probe failed for %s: %s", app_key, exc)
            findings.append(
                Finding(
                    id=f"recovery.custom_volume_verify.{app_key}",
                    physics=f"restore-verify results for custom volumes of app {app_key}",
                    verdict=Verdict.INDETERMINATE,
                    summary="custom_volume_verify probe raised unexpectedly",
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

    # Probe 2d: scheduled-backup timer alive (#868 P3) — SYSTEM-LEVEL, runs once outside the
    # per-app loop (the ms-backup.timer drives `--all`, not a single app). This establishes the
    # post-loop system-probe seam #1281's platform-DB verify probe reuses.
    try:
        findings.append(_probe_backup_schedule_overdue())
    except Exception as exc:
        log.warning("backup_schedule_overdue probe failed: %s", exc)
        findings.append(
            Finding(
                id="recovery.backup_schedule_overdue",
                physics="ms-backup.timer systemd unit active-state",
                verdict=Verdict.INDETERMINATE,
                summary="backup_schedule_overdue probe raised unexpectedly",
                detail=f"{type(exc).__name__}: {exc}",
            )
        )

    # Probe 2e: platform-DB restore-verify result (#1281) — SYSTEM-LEVEL, runs once outside the
    # per-app loop. The per-app verify probe never sees the platform DB's own backup (fixed
    # <config_root>/backups/_platform dir, no app key), so its verify verdict — incl. DRIFT ("the
    # platform backup does not restore") — was previously written to a sidecar nothing surfaced.
    try:
        f = _probe_platform_backup_verify_result(resolved_config_root)
        if f is not None:
            findings.append(f)
    except Exception as exc:
        log.warning("platform_backup_verify probe failed: %s", exc)
        findings.append(
            Finding(
                id="recovery.platform_backup_verify",
                physics="restore-verify result for the SLOP platform DB backup",
                verdict=Verdict.INDETERMINATE,
                summary="platform_backup_verify probe raised unexpectedly",
                detail=f"{type(exc).__name__}: {exc}",
            )
        )

    return findings


__all__ = ["reconcile_recovery"]
