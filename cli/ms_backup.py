#!/usr/bin/env python3
"""ms_backup — operational backup EXECUTE + restore-verify CLI (#868 P1b).

The operational tier of the SLOP backup product. It is deliberately OUTSIDE ``backend/agent/``:
the SLOP agent is runtime-only / observe-only (two-owner firewall, CLAUDE.md "Knowledge-Lifecycle")
— it READS a verify result, it never RUNS a backup or a restore. Tar-creation and tar-extraction
are *actions*, so they live here (and in ``backend.platform.backup_ops``), not in the agent.

What it does (docs/BACKUP-PRODUCT-868-DESIGN.md §8 P1b):
  * EXECUTE a config-volume backup (tar ``<config_root>/<key>``) and/or the SLOP platform DB
    (a consistent SQLite snapshot of ``<data_dir>/state.db``);
  * immediately VERIFY each artifact by restoring it to scratch and applying the per-class
    invariant (generic "≥1 file" for config, ``PRAGMA integrity_check`` for the DB, or a per-app
    ``backup_verify`` sentinel) — so the "last-restore-verify" red-signal accrues from day one;
  * write the GROUND ``.verify-*.json`` sidecar the recovery agent reads.

A ``DRIFT`` verdict — "you thought you had a backup; you do not" — is the highest-value thing the
product emits, and is what makes the ``_probe_backup_verify_result`` recovery probe able to go red.

Usage:
  ms-backup <app>             EXECUTE + verify one app's config backup
  ms-backup <app> --execute   (explicit) same as above
  ms-backup <app> --verify    re-verify the LATEST existing artifact (no new backup)
  ms-backup --all             EXECUTE + verify every backup_supported app + the platform DB
  ms-backup --platform        EXECUTE + verify the SLOP platform DB only
  ms-backup --dry-run ...     print what WOULD run; touch nothing

Exit status: 0 if every executed/verified artifact verdict is ``verified``; 1 if any ``DRIFT``;
2 if any ``INDETERMINATE`` (unresolvable configuration, off-host refusal, missing source) or a
usage/resolution error. (A ``DRIFT`` exit is intentional — a failed restore-verify is a
real failure the operator/CI must see.)
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

from backend.platform import backup_ops

# SoT for the platform backup subdir (<config_root>/backups/_platform) lives in backup_ops.
from backend.platform.backup_ops import PLATFORM_BACKUP_SUBDIR

PLATFORM_ARCNAME = "state.db"  # the snapshot's basename → the relpath the DB invariant keys on


def _resolve_backup_dir(manifest: Any, config_root: str) -> str:
    """Backup directory for *manifest*: explicit ``backup_dir`` wins, else ``<config_root>/backups/<key>``."""
    explicit = getattr(manifest, "backup_dir", "") or ""
    if explicit:
        return str(explicit)
    key = getattr(manifest, "key", "") or ""
    return str(Path(config_root) / "backups" / key) if (config_root and key) else ""


def _read_offhost_settings() -> tuple[str, str]:
    """Read (rclone_remote_name, operator_age_recipient_pubkey) from the platform DB settings.

    Both empty on any miss → the caller treats it as "off-host not configured" and refuses LOUDLY
    (never a silent local-only fallback for an app that REQUIRES off-host). The recipient is an age
    recipient PUBLIC key by contract (``execute_offhost_backup`` fail-closes on an identity/secret
    key) — so it lives in settings, never in the ``.env`` secret store: SLOP holds no decryption
    material (design §6/§13)."""
    from backend.core.state import StateDB

    with StateDB() as db:
        remote = (db.get_setting("backup_offhost_remote", "") or "").strip()
        recipient = (db.get_setting("backup_offhost_recipient", "") or "").strip()
    return remote, recipient


def _offhost_or_empty(needed: bool) -> tuple[str, str]:
    """Resolve (remote, recipient) for a run; reads settings only when off-host is in play. A
    settings-read miss degrades LOUDLY to ("", "") — which makes the off-host path REFUSE
    (INDETERMINATE), never silently fall back to a local-only backup for a require-off-host app."""
    if not needed:
        return "", ""
    try:
        return _read_offhost_settings()
    except Exception as exc:
        print(
            f"[warn] could not read off-host settings ({exc}); off-host will refuse (unconfigured)",
            file=sys.stderr,
        )
        return "", ""


def _backup_app_offhost(
    key: str,
    source: Path,
    bdir: str,
    backup_verify: str,
    remote: str,
    recipient: str,
    *,
    dry_run: bool = False,
    keep: int = 0,
) -> tuple[str, str, Path | None]:
    """Off-host backup path (#1303): wire ``backup_offhost.execute_offhost_backup`` (local tar →
    age dual-recipient encrypt → rclone upload → ephemeral in-window restore-verify → OFFHOST-scope
    sidecar) for an app that REQUIRES an off-host copy (manifest ``backup_offhost: true``).

    Refuses LOUDLY (``INDETERMINATE``) when the remote/recipient are unconfigured — never a silent
    local-only backup, which would leave the app's recoverability probe RED-on-local-only forever
    (the exact gap #1303 closes). The credential firewall (recipient must be an ``age1…`` PUBLIC
    key; remote a bare name) is enforced by the engine and surfaces here as a fail-closed verdict."""
    if not (remote and recipient):
        return (
            backup_ops.VERDICT_INDETERMINATE,
            "off-host REQUIRED but unconfigured — set backup_offhost_remote + "
            "backup_offhost_recipient (an age1… PUBLIC key) in settings; refusing local-only",
            None,
        )
    if dry_run:
        return (
            "DRY-RUN",
            f"would off-host backup {source} → rclone:{remote} "
            "(age dual-recipient encrypt + ephemeral restore-verify)",
            None,
        )
    if not source.exists():
        return (backup_ops.VERDICT_INDETERMINATE, f"source config dir not found: {source}", None)
    from backend.platform import backup_offhost

    invariant = backup_ops.invariant_for(backup_verify)
    try:
        artifact, verdict, reason = backup_offhost.execute_offhost_backup(
            source,
            bdir,
            key,
            target=f"rclone:{remote}",
            operator_recipient=recipient,
            invariant=invariant,
        )
    except backup_offhost.OffhostError as exc:
        return (backup_ops.VERDICT_INDETERMINATE, f"off-host backup failed: {exc}", None)
    except ValueError as exc:  # bad target / recipient (e.g. an identity key) — fail-closed
        return (backup_ops.VERDICT_INDETERMINATE, f"off-host config rejected: {exc}", None)
    if keep > 0:
        pruned = backup_ops.prune_backups(bdir, keep=keep)
        if pruned:
            reason = f"{reason}; pruned {len(pruned)} old backup(s) (keep={keep})"
    return (verdict, reason, artifact)


def backup_app(
    key: str,
    config_root: str,
    *,
    backup_verify: str = "",
    backup_dir: str = "",
    dry_run: bool = False,
    keep: int = 0,
    off_host: bool = False,
    offhost_remote: str = "",
    offhost_recipient: str = "",
) -> tuple[str, str, Path | None]:
    """EXECUTE + verify a single app's config-volume backup.

    Tars ``<config_root>/<key>`` to ``<backup_dir>/<key>-<ts>.tar.gz`` (atomic), restores it to
    scratch applying the *backup_verify* invariant, and writes the verify sidecar. Returns
    ``(verdict, reason, artifact_path)``. The source-missing / write-error cases come back as
    ``INDETERMINATE`` (LOUD), never a claimed success.

    When *off_host* is set (an app's manifest ``backup_offhost: true`` or the ``--off-host`` force
    flag), the backup is routed through :func:`_backup_app_offhost` instead — which produces the
    SAME local recoverable copy PLUS an encrypted off-host copy and an off-host-scope verify (#1303).

    *keep* > 0 fail-safe-prunes the backup dir AFTER the new verdict is recorded (§3 retention) —
    older artifacts are dropped only when a NEWER restore-verified backup exists. ``keep=0`` (the
    default, used by unit tests of the pure execute) prunes nothing.
    """
    bdir = backup_dir or str(Path(config_root) / "backups" / key)
    source = Path(config_root) / key
    if off_host:
        return _backup_app_offhost(
            key,
            source,
            bdir,
            backup_verify,
            offhost_remote,
            offhost_recipient,
            dry_run=dry_run,
            keep=keep,
        )
    if dry_run:
        return (
            "DRY-RUN",
            f"would tar {source} → {bdir}/{key}-<ts>.tar.gz then restore-verify",
            None,
        )
    if not source.exists():
        return (backup_ops.VERDICT_INDETERMINATE, f"source config dir not found: {source}", None)
    try:
        artifact = backup_ops.execute_backup(source, bdir, key)
    except Exception as exc:  # OSError / tarfile.TarError — a real execute failure
        return (backup_ops.VERDICT_INDETERMINATE, f"backup EXECUTE failed: {exc}", None)
    invariant = backup_ops.invariant_for(backup_verify)
    verdict, reason = backup_ops.verify_backup_artifact(artifact, invariant=invariant)
    backup_ops.write_verify_sidecar(bdir, artifact.name, verdict)
    if keep > 0:
        pruned = backup_ops.prune_backups(bdir, keep=keep)
        if pruned:
            reason = f"{reason}; pruned {len(pruned)} old backup(s) (keep={keep})"
    return (verdict, reason, artifact)


def _resolve_volume_host_path(host_path: str, config_root: str, media_root: str) -> str:
    """Resolve a ``VolumeDef.host_path`` to an absolute filesystem path (#1287, design §14).

    Mirrors ``executor._expand_path`` token handling: ``{config_root}``/``{media_root}`` are
    expanded; an ABSOLUTE result is used as-is (the design's sanctioned way to reference a path
    OUTSIDE config_root — e.g. a media library); a bare RELATIVE path is taken relative to
    ``config_root`` and MUST stay under it. A relative path that escapes config_root via ``..``
    (review BLOCKER) is malformed → returns ``""`` so the caller emits an ``INDETERMINATE`` row
    (LOUD), never a silent skip. ``""`` is also returned when unresolvable (no config_root and not
    absolute). (Manifest trust for community ``custom_volumes`` is a separate, pre-existing concern
    owned at manifest-validation time — absolute paths already reference anywhere by design; this
    only stops an UNINTENTIONAL traversal from a path declared relative.)
    """
    if not host_path:
        return ""
    expanded = host_path.replace("{config_root}", config_root or "").replace(
        "{media_root}", media_root or ""
    )
    p = Path(expanded)
    if p.is_absolute():
        return str(p)
    if not config_root:
        return ""
    base = Path(config_root).resolve()
    candidate = (base / expanded).resolve()
    if candidate != base and base not in candidate.parents:
        return (
            ""  # relative host_path escapes config_root via traversal — malformed (use an abs path)
        )
    return str(candidate)


# A backed-up CONFIG-class custom volume lives under a `volumes/<idx>` SUBDIR of the app's backup dir,
# ISOLATED from the primary config backup: latest_verify_result reads ONE dir and the newest sidecar
# wins, so a volume's verdict sharing the app dir could MASK a config-backup DRIFT (the #1281-class
# silent-green). Surfacing these per-volume verdicts via a recovery probe is a #1281-class follow-on
# (filed), out of this execute loop's scope.
_VOLUME_SUBDIR = "volumes"


def backup_custom_volumes(
    manifest: Any,
    config_root: str,
    media_root: str = "",
    *,
    dry_run: bool = False,
    keep: int = 0,
) -> list[tuple[str, str, str]]:
    """EXECUTE + verify each CONFIG-class (or untagged) custom volume of *manifest* (#1287, §14).

    Per design §14 ``backup_class`` policy:
      * ``config`` / untagged (loader coerces unknown→config, fail-safe) → full tar + verify + sidecar;
      * ``media``  → SKIP the (large, re-acquirable) bytes — the index rides the app's primary config
        backup / its ``backup_verify`` (and recovery_audit DRIFTs a media volume declaring no index);
      * ``exclude`` → SKIP (operator-declared throwaway).

    Each backed-up volume is isolated under ``<backup_dir>/volumes/<idx>/`` and restore-verified with
    the GENERIC "≥1 file restored" invariant. The per-app DECLARED ``backup_verify`` index invariant is
    NOT applied here: it is owned by the primary config backup (``backup_app``) and §14(iv) scopes it
    to the media-metadata case (whose bytes this loop skips); applying it to an arbitrary config
    volume that does not contain that index would falsely DRIFT. Returns ``(label, verdict, reason)``
    rows (one per backed-up volume); skipped / unresolved-``<prompt:>``-sentinel volumes yield no row.
    A source-missing / write error comes back ``INDETERMINATE`` (LOUD), never a claimed success.
    """
    key = getattr(manifest, "key", "") or ""
    base_bdir = _resolve_backup_dir(manifest, config_root)
    rows: list[tuple[str, str, str]] = []
    for idx, vol in enumerate(getattr(manifest, "custom_volumes", []) or []):
        bclass = (getattr(vol, "backup_class", "config") or "config").strip()
        if bclass in ("media", "exclude"):
            continue
        if getattr(vol, "prompt_key", ""):
            continue  # unresolved <prompt:{key}> sentinel — not a real path in the template
        label = f"app:{key} vol[{idx}]:{getattr(vol, 'container_path', '') or bclass}"
        host = _resolve_volume_host_path(getattr(vol, "host_path", ""), config_root, media_root)
        if not host:
            rows.append((label, backup_ops.VERDICT_INDETERMINATE, "volume host_path unresolvable"))
            continue
        vol_bdir = str(Path(base_bdir) / _VOLUME_SUBDIR / str(idx))
        if dry_run:
            rows.append(
                (
                    label,
                    "DRY-RUN",
                    f"would tar {host} → {vol_bdir}/{key}-vol{idx}-<ts>.tar.gz then verify",
                )
            )
            continue
        if not Path(host).exists():
            rows.append(
                (label, backup_ops.VERDICT_INDETERMINATE, f"volume source not found: {host}")
            )
            continue
        try:
            artifact = backup_ops.execute_backup(host, vol_bdir, f"{key}-vol{idx}")
        except Exception as exc:  # OSError / tarfile.TarError — a real execute failure
            rows.append(
                (label, backup_ops.VERDICT_INDETERMINATE, f"volume backup EXECUTE failed: {exc}")
            )
            continue
        verdict, reason = backup_ops.verify_backup_artifact(
            artifact, invariant=backup_ops.invariant_for("")
        )
        backup_ops.write_verify_sidecar(vol_bdir, artifact.name, verdict)
        if keep > 0:
            pruned = backup_ops.prune_backups(vol_bdir, keep=keep)
            if pruned:
                reason = f"{reason}; pruned {len(pruned)} old backup(s) (keep={keep})"
        rows.append((label, verdict, reason))
    return rows


def _platform_db_invariant(arcname: str) -> backup_ops.Invariant:
    """Platform-DB restore invariant (design §2a): the restored DB opens + passes
    ``PRAGMA integrity_check`` (reuses :func:`backup_ops.sqlite_db_invariant`) AND its recorded
    schema head equals the code's expected migration head.

    The schema-head leg (the §2a refinement the generic sqlite invariant deferred): a platform DB
    that opens cleanly but is BEHIND the current migration head is a stale/incomplete backup — a
    new-hardware restore from it would be missing schema, so it is **DRIFT**, not a recoverable
    backup. GROUND: reconciles ``MAX(version)`` in the restored DB's ``schema_migrations`` against
    the canonical ``_scan_migrations`` head (reused, not reimplemented, so the two cannot diverge).
    An empty/absent ``schema_migrations`` is DRIFT (no provable head), never a silent pass."""
    integrity = backup_ops.sqlite_db_invariant(arcname)

    def _check(scratch: Path) -> tuple[bool, str]:
        ok, reason = integrity(scratch)
        if not ok:
            return ok, reason
        # Lazy import — keep the migrations SoT off the module-load path.
        from backend.core.migrations import _DEFAULT_MIGRATIONS_DIR, _scan_migrations

        db = scratch / arcname
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                row = con.execute("SELECT MAX(version) FROM schema_migrations").fetchone()
            finally:
                con.close()
        except sqlite3.Error as exc:
            return False, f"restored platform DB has no readable schema_migrations table: {exc}"
        restored_head = row[0] if row and row[0] is not None else None
        if restored_head is None:
            return (
                False,
                "restored platform DB records no applied migrations (empty schema_migrations)",
            )
        expected_head = max(
            (m.version for m in _scan_migrations(_DEFAULT_MIGRATIONS_DIR)), default=None
        )
        if expected_head is not None and restored_head != expected_head:
            return (
                False,
                f"restored platform DB at schema v{restored_head}, expected migration head "
                f"v{expected_head} — stale/incomplete backup",
            )
        return True, f"{reason}; schema head v{restored_head} == expected"

    return _check


def backup_platform_db(
    db_path: str,
    backup_dir: str,
    *,
    dry_run: bool = False,
    keep: int = 0,
) -> tuple[str, str, Path | None]:
    """EXECUTE + verify a consistent backup of the SLOP platform DB (§2a).

    Takes a consistent online snapshot (``snapshot_sqlite``) so a live DB's -wal/-shm never yield a
    torn backup, tars the snapshot (arcname ``state.db``), then restore-verifies it with the
    :func:`_platform_db_invariant` — ``PRAGMA integrity_check`` AND the recorded schema head equals
    the code's expected migration head (§2a): a DB that opens but is behind the head is a
    stale/incomplete backup, still DRIFT. Returns ``(verdict, reason, artifact_path)``. *keep* > 0
    fail-safe-prunes after the verdict is recorded (§3), same gate as :func:`backup_app`.
    """
    dbp = Path(db_path)
    if dry_run:
        return (
            "DRY-RUN",
            f"would snapshot {dbp} → {backup_dir}/platform-db-<ts>.tar.gz then verify",
            None,
        )
    if not dbp.is_file():
        return (backup_ops.VERDICT_INDETERMINATE, f"platform DB not found: {dbp}", None)
    try:
        # Stage a consistent snapshot named state.db so the restored relpath matches the invariant.
        with tempfile.TemporaryDirectory(prefix="ms-backup-snap-") as td:
            snap = Path(td) / PLATFORM_ARCNAME
            backup_ops.snapshot_sqlite(dbp, snap)
            artifact = backup_ops.execute_backup(snap, backup_dir, "platform-db")
    except Exception as exc:  # sqlite3.Error / OSError / tarfile.TarError
        return (backup_ops.VERDICT_INDETERMINATE, f"platform-DB backup EXECUTE failed: {exc}", None)
    verdict, reason = backup_ops.verify_backup_artifact(
        artifact, invariant=_platform_db_invariant(PLATFORM_ARCNAME)
    )
    backup_ops.write_verify_sidecar(backup_dir, artifact.name, verdict)
    if keep > 0:
        pruned = backup_ops.prune_backups(backup_dir, keep=keep)
        if pruned:
            reason = f"{reason}; pruned {len(pruned)} old backup(s) (keep={keep})"
    return (verdict, reason, artifact)


def verify_app_latest(
    key: str,
    config_root: str,
    *,
    backup_verify: str = "",
    backup_dir: str = "",
) -> tuple[str, str, Path | None]:
    """Re-verify the NEWEST existing artifact for *key* without making a new backup.

    Restores the latest ``*.tar.gz`` in the backup dir (ignoring ``.tmp`` partials and ``.verify-``
    sidecars) and rewrites its sidecar. ``INDETERMINATE`` when no artifact exists. This is the
    ``ms-backup <app> --verify`` path — an on-demand restore drill against what is already stored.
    """
    bdir = Path(backup_dir or (str(Path(config_root) / "backups" / key)))
    try:
        artifacts = [
            a
            for a in bdir.iterdir()
            if a.is_file() and a.name.endswith(".tar.gz") and not a.name.startswith(".verify-") and not a.name.endswith(".tmp")
        ]
    except OSError as exc:
        return (backup_ops.VERDICT_INDETERMINATE, f"backup dir unreadable: {exc}", None)
    if not artifacts:
        return (backup_ops.VERDICT_INDETERMINATE, f"no backup artifact to verify in {bdir}", None)
    latest = max(artifacts, key=lambda a: a.stat().st_mtime)
    invariant = backup_ops.invariant_for(backup_verify)
    verdict, reason = backup_ops.verify_backup_artifact(latest, invariant=invariant)
    backup_ops.write_verify_sidecar(str(bdir), latest.name, verdict)
    return (verdict, reason, latest)


def verify_platform_latest(
    backup_dir: str,
    *,
    dry_run: bool = False,
) -> tuple[str, str, Path | None]:
    """Re-verify the NEWEST existing platform-DB artifact without making a new backup.

    Restores the latest ``*.tar.gz`` in the platform backup dir (ignoring ``.tmp`` partials and
    ``.verify-`` sidecars), applies the :func:`_platform_db_invariant`, and rewrites its sidecar.
    ``INDETERMINATE`` when no artifact exists. This is the ``--platform --verify`` path."""
    bdir = Path(backup_dir)
    if dry_run:
        return ("DRY-RUN", f"would re-verify the latest platform-DB artifact in {bdir}", None)
    try:
        artifacts = [
            a
            for a in bdir.iterdir()
            if a.is_file()
            and a.name.endswith(".tar.gz")
            and not a.name.startswith(".verify-")
            and not a.name.endswith(".tmp")
        ]
    except OSError as exc:
        return (backup_ops.VERDICT_INDETERMINATE, f"platform backup dir unreadable: {exc}", None)
    if not artifacts:
        return (
            backup_ops.VERDICT_INDETERMINATE,
            f"no platform backup artifact to verify in {bdir}",
            None,
        )
    latest = max(artifacts, key=lambda a: a.stat().st_mtime)
    verdict, reason = backup_ops.verify_backup_artifact(
        latest, invariant=_platform_db_invariant(PLATFORM_ARCNAME)
    )
    backup_ops.write_verify_sidecar(str(bdir), latest.name, verdict)
    return (verdict, reason, latest)


# ── verdict → exit code + reporting ─────────────────────────────────────────────────────────


def _is_failure(verdict: str) -> bool:
    return verdict == backup_ops.VERDICT_DRIFT


# ── #1284: per-app cadence for the scheduled --due-only run ──────────────────────────────────
# The hourly ms-backup.timer calls `--all --due-only`; the cadence decides which apps actually
# back up this hour. Cadence source: the `backup_cadence_hours` setting (global default) with
# optional per-app overrides in the `backup_cadence_overrides` JSON setting. Decoupling cadence
# from the timer's OnCalendar means changing cadence is a settings write — no systemd unit re-render.
_DEFAULT_CADENCE_HOURS = 24.0
_SKIPPED = "skipped"  # benign verdict (not a DRIFT failure) for an app inside its cadence window


def _cadence_for(app_key: str, global_hours: float, overrides: dict) -> float:
    """Per-app cadence: an explicit override (hours) else the global default."""
    try:
        return float(overrides.get(app_key, global_hours))
    except (TypeError, ValueError):
        return global_hours


def _app_is_due(backup_dir: str, cadence_hours: float) -> bool:
    """True if a scheduled backup is DUE: never backed up, or the newest backup is at least
    *cadence_hours* old. Reuses ``backend.agent.backup.latest_backup_age_hours`` — the same GROUND
    mtime the recovery freshness probe reads — so the due-check and the probe can never disagree on
    "how old is the latest backup" (no duplicated artifact-scan)."""
    from backend.agent.backup import latest_backup_age_hours

    age = latest_backup_age_hours(backup_dir)
    return age is None or age >= cadence_hours


def _read_cadence_settings() -> tuple[float, dict]:
    """Read (global_cadence_hours, per_app_overrides) from the platform DB; defaults on any failure
    (cadence still applies at the 24h default — a settings-read miss never disables backups)."""
    import json as _json

    from backend.core.state import StateDB

    global_h = _DEFAULT_CADENCE_HOURS
    overrides: dict = {}
    with StateDB() as db:
        raw_h = db.get_setting("backup_cadence_hours", str(int(_DEFAULT_CADENCE_HOURS)))
        global_h = float(raw_h or _DEFAULT_CADENCE_HOURS)
        overrides = _json.loads(db.get_setting("backup_cadence_overrides", "{}") or "{}")
    return global_h, overrides


def _cadence_or_default(due_only: bool) -> tuple[float, dict]:
    """Resolve (global_hours, overrides) for a run; a settings-read miss degrades to the 24h
    default LOUDLY (never silently disables backups). No-op (defaults) when not --due-only."""
    if not due_only:
        return _DEFAULT_CADENCE_HOURS, {}
    try:
        return _read_cadence_settings()
    except Exception as exc:
        print(
            f"[warn] could not read backup cadence settings ({exc}); "
            f"using {_DEFAULT_CADENCE_HOURS:.0f}h default",
            file=sys.stderr,
        )
        return _DEFAULT_CADENCE_HOURS, {}


def _backup_all(
    manifests: list,
    config_root: str,
    media_root: str,
    args,
    cadence_global: float,
    cadence_overrides: dict,
    offhost_remote: str = "",
    offhost_recipient: str = "",
) -> list[tuple[str, str, str]]:
    """The ``--all`` backup loop with #1284 ``--due-only`` cadence gating. Returns the
    (label, verdict, reason) rows; an app inside its cadence window yields a benign ``skipped``.
    Each app's config-class custom volumes (#1287) ride the same run right after its config backup.

    An app is backed up off-host (#1303) when its manifest sets ``backup_offhost: true`` OR the run
    carries ``--off-host``; otherwise local-only as before."""
    rows: list[tuple[str, str, str]] = []
    for m in manifests:
        bdir = _resolve_backup_dir(m, config_root)
        if args.due_only and not _app_is_due(
            bdir, _cadence_for(m.key, cadence_global, cadence_overrides)
        ):
            rows.append((f"app:{m.key}", _SKIPPED, "not due — within cadence window"))
            continue
        v, r, _ = backup_app(
            m.key,
            config_root,
            backup_verify=getattr(m, "backup_verify", ""),
            backup_dir=bdir,
            dry_run=args.dry_run,
            keep=args.keep,
            off_host=getattr(args, "off_host", False) or bool(getattr(m, "backup_offhost", False)),
            offhost_remote=offhost_remote,
            offhost_recipient=offhost_recipient,
        )
        rows.append((f"app:{m.key}", v, r))
        rows.extend(
            backup_custom_volumes(m, config_root, media_root, dry_run=args.dry_run, keep=args.keep)
        )
    return rows


def _print(label: str, verdict: str, reason: str) -> None:
    print(f"[{verdict}] {label}: {reason}")


# ── dispatch (resolves ground-truth config_root + DB path, then calls the core funcs) ────────


def _resolve_runtime() -> tuple[str, str, str, list[Any]]:
    """Resolve (config_root, media_root, platform_db_path, backup_supported_manifests) from physics.

    Reads config_root + media_root from the platform DB and the DB path from the runtime config;
    loads each installed app's manifest to find ``backup_supported``. media_root is needed to expand
    the ``{media_root}`` token in a custom volume's host_path (#1287). Kept out of the pure core
    funcs so they stay unit-testable without a live DB.
    """
    from backend.core.config import config as _cfg
    from backend.core.state import StateDB, configure

    configure(_cfg.db_path)
    manifests: list[Any] = []
    with StateDB() as db:
        platform = db.get_platform()
        config_root = getattr(platform, "config_root", "") or ""
        media_root = getattr(platform, "media_root", "") or ""
        apps = db.get_all_apps()
    from backend.core.logging import get_logger
    from backend.manifests.loader import load_manifest

    log = get_logger(__name__)
    for app in apps:
        try:
            m = load_manifest(app.key)
        except Exception as exc:
            log.debug("ms-backup: skipping %s (manifest load failed): %s", app.key, exc)
            continue
        if getattr(m, "backup_supported", False):
            manifests.append(m)
    return config_root, media_root, str(_cfg.db_path), manifests


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ms-backup", description="SLOP backup execute + restore-verify"
    )
    parser.add_argument("app", nargs="?", help="app key to back up (omit with --all / --platform)")
    parser.add_argument(
        "--all", action="store_true", help="back up every backup_supported app + the platform DB"
    )
    parser.add_argument("--platform", action="store_true", help="back up the SLOP platform DB only")
    parser.add_argument("--execute", action="store_true", help="execute a backup (default action)")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="re-verify the latest existing artifact (no new backup)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print what would run; touch nothing"
    )
    parser.add_argument(
        "--due-only",
        action="store_true",
        help="with --all: back up only apps whose newest backup is older than their cadence "
        "(backup_cadence_hours setting, default 24h; per-app overrides via backup_cadence_overrides). "
        "Used by the hourly ms-backup.timer so cadence is a settings write, not a unit re-render.",
    )
    parser.add_argument(
        "--keep",
        type=int,
        default=7,
        help="retention: keep this many newest backups; older ones are fail-safe-pruned after a "
        "newer restore-verified backup exists (default 7; 0 disables pruning)",
    )
    parser.add_argument(
        "--off-host",
        dest="off_host",
        action="store_true",
        help="force off-host backup for this run (age-encrypt + rclone-upload + ephemeral verify) "
        "for every app, not just apps whose manifest sets backup_offhost:true. Requires "
        "backup_offhost_remote + backup_offhost_recipient (age1… PUBLIC key) in settings.",
    )
    return parser


def _plan_platform_backup(
    args: argparse.Namespace,
    platform_bdir: str,
    db_path: str,
    cadence_global: float,
    results: list[tuple[str, str, str]],
) -> None:
    if args.platform and args.verify:
        v, r, _ = verify_platform_latest(platform_bdir, dry_run=args.dry_run)
        results.append(("platform-db (verify-only)", v, r))
    elif not (args.verify and args.app) and (args.all or args.platform):
        if args.all and args.due_only and not _app_is_due(platform_bdir, cadence_global):
            results.append(
                (
                    "platform-db",
                    _SKIPPED,
                    f"not due — newest platform backup within {cadence_global:.0f}h cadence; "
                    "run `ms-backup --platform` to force",
                )
            )
        else:
            v, r, _ = backup_platform_db(
                db_path, platform_bdir, dry_run=args.dry_run, keep=args.keep
            )
            results.append(("platform-db", v, r))


def _summarize_results(results: list[tuple[str, str, str]]) -> int:
    failed = False
    indeterminate = False
    for label, verdict, reason in results:
        _print(label, verdict, reason)
        if _is_failure(verdict):
            failed = True
        elif verdict == backup_ops.VERDICT_INDETERMINATE:
            indeterminate = True
    return 2 if indeterminate else (1 if failed else 0)


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if not (args.app or args.all or args.platform):
        parser.error("specify an app key, --all, or --platform")

    try:
        config_root, media_root, db_path, manifests = _resolve_runtime()
    except Exception as exc:
        print(f"[ERROR] could not resolve runtime state (config_root / DB): {exc}", file=sys.stderr)
        return 2

    results: list[tuple[str, str, str]] = []  # (label, verdict, reason)
    platform_bdir = str(Path(config_root) / "backups" / PLATFORM_BACKUP_SUBDIR)
    cadence_global, cadence_overrides = _cadence_or_default(args.due_only)

    if args.verify and args.app:
        m = next((x for x in manifests if getattr(x, "key", "") == args.app), None)
        bv = getattr(m, "backup_verify", "") if m else ""
        bd = _resolve_backup_dir(m, config_root) if m else ""
        v, r, _ = verify_app_latest(args.app, config_root, backup_verify=bv, backup_dir=bd)
        results.append((f"app:{args.app} (verify-only)", v, r))
    elif args.all:
        need_offhost = bool(getattr(args, "off_host", False)) or any(
            getattr(m, "backup_offhost", False) for m in manifests
        )
        oh_remote, oh_recipient = _offhost_or_empty(need_offhost)
        results.extend(
            _backup_all(
                manifests,
                config_root,
                media_root,
                args,
                cadence_global,
                cadence_overrides,
                oh_remote,
                oh_recipient,
            )
        )
    elif args.app:
        m = next((x for x in manifests if getattr(x, "key", "") == args.app), None)
        app_offhost = bool(getattr(args, "off_host", False)) or bool(
            getattr(m, "backup_offhost", False)
        )
        oh_remote, oh_recipient = _offhost_or_empty(app_offhost)
        v, r, _ = backup_app(
            args.app,
            config_root,
            backup_verify=getattr(m, "backup_verify", "") if m else "",
            backup_dir=_resolve_backup_dir(m, config_root) if m else "",
            dry_run=args.dry_run,
            keep=args.keep,
            off_host=app_offhost,
            offhost_remote=oh_remote,
            offhost_recipient=oh_recipient,
        )
        results.append((f"app:{args.app}", v, r))
        if m is not None:
            results.extend(
                backup_custom_volumes(
                    m, config_root, media_root, dry_run=args.dry_run, keep=args.keep
                )
            )

    _plan_platform_backup(
        args, platform_bdir, db_path, cadence_global, results
    )

    return _summarize_results(results)


if __name__ == "__main__":
    sys.exit(main())
