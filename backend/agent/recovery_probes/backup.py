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

from backend.agent.backup import (
    app_backup_dir,
    latest_verify_result,
    latest_verify_scope,
    platform_backup_dir,
)
from backend.agent.spine import Finding, Verdict

# design §13: the only scope that proves an off-host copy decrypts. Anything else (including a
# missing scope on a pre-§13 sidecar) leaves off-host recoverability UNPROVEN.
_OFFHOST_VERIFY_SCOPE = "offhost-ciphertext-roundtrip"

_BACKUP_WARN_H = 24  # hours — DRIFT (warn)
_BACKUP_CRIT_H = 72  # hours — DRIFT (crit)

# Map a restore-verify sidecar verdict string (written by backend.platform.backup_ops) to the
# spine Verdict.  Kept here, not imported from the operational layer, to preserve the
# observe-only agent firewall (the agent reads results, never runs the restore).
_VERIFY_VERDICT_MAP = {
    "verified": Verdict.VERIFIED,
    "DRIFT": Verdict.DRIFT,
    "INDETERMINATE": Verdict.INDETERMINATE,
}


def _is_backup_artifact(p: Path) -> bool:
    """True if *p* is a real backup artifact, not a verify sidecar or a .tmp partial.

    The backup dir holds both ``<name>.tar.gz`` artifacts AND ``.verify-*.json`` restore-verify
    sidecars. Freshness must count only the former — a sidecar's mtime is not a backup's age, and a
    dir left with only a sidecar (its artifact lost/pruned) is "no backup", not a fresh one. The
    ``.verify-`` prefix is the same one pinned by tests/test_backup_ops.py::test_sidecar_prefix_contract.
    """
    return p.is_file() and not p.name.startswith(".verify-") and not p.name.endswith(".tmp")


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
        # Count BACKUP ARTIFACTS only — exclude the .verify-*.json restore-verify sidecars (and
        # any .tmp partial). A dir holding only a sidecar (its .tar.gz pruned/lost) must read as
        # "no backup", NOT as a fresh one whose age is the sidecar's mtime (the silent-green the
        # adversarial review caught: freshness would falsely VERIFY a vanished backup).
        artifacts = [a for a in bdir.iterdir() if _is_backup_artifact(a)]
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
            summary="backup_dir has no backup artifacts (only sidecars/none)",
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


def _probe_backup_verify_result(app: Any, config_root: str | None = None) -> Finding | None:
    """Probe 2c: restore-verify result — the KEYSTONE recoverability signal (#868 §9).

    Freshness (2b) only proves a backup is RECENT; it cannot prove the backup is RESTORABLE.
    This probe surfaces the verdict that ``backend.platform.backup_ops.verify_backup_artifact``
    recorded in a sidecar (read GROUND, observe-only, via
    :func:`backend.agent.backup.latest_verify_result`): a ``DRIFT`` here means "you have a recent
    backup that does NOT restore" — the single most valuable thing the product can tell you.

    Returns None when no backup dir resolves (owned by ``_probe_backup_configured``) OR when no
    verify has run yet (no sidecar) — the latter avoids noise before the ``ms-backup`` verify path
    (P1b) exists; once verify-after-execute is wired, every fresh backup carries a verdict.
    """
    app_key: str = getattr(app, "key", str(app))
    finding_id = f"recovery.backup_verify.{app_key}"
    physics = f"restore-verify result for app {app_key}"

    backup_dir = app_backup_dir(app, config_root)
    if not backup_dir:
        return None  # no backup dir resolvable — verify has nothing to report

    verdict_str, age_h = latest_verify_result(backup_dir)
    if verdict_str is None:
        return None  # no restore-verify has run yet — silent (no noise) until P1b wires it

    verdict = _VERIFY_VERDICT_MAP.get(verdict_str, Verdict.INDETERMINATE)
    age_txt = f"{age_h:.0f}h ago" if age_h is not None else "unknown age"

    # design §13 RED-on-local-only: an app that REQUIRES an off-host copy is NOT recovery-verified
    # by a local-plaintext-only verify — that scope never proved the off-host ciphertext decrypts.
    # Refuse VERIFIED (downgrade to DRIFT) while the latest scope is anything but offhost-roundtrip,
    # EVEN when the local verdict is "verified". A missing scope (pre-§13 sidecar) reads as
    # local-plaintext (conservative) via latest_verify_scope, so it also trips this — never a silent
    # off-host-verified. (A DRIFT/INDETERMINATE local verdict already surfaces; this only tightens a
    # would-be VERIFIED.)
    if getattr(app, "backup_offhost", False) and verdict is Verdict.VERIFIED:
        scope = latest_verify_scope(backup_dir)
        if scope != _OFFHOST_VERIFY_SCOPE:
            return Finding(
                id=finding_id,
                physics=physics,
                verdict=Verdict.DRIFT,
                summary=(
                    f"off-host backup recoverability UNPROVEN — latest verify scope is "
                    f"{scope or 'none'} ({age_txt}), not off-host ciphertext round-trip"
                ),
                detail=(
                    f"path={backup_dir} verify_result={verdict_str} verify_scope={scope} "
                    f"requires=offhost-ciphertext-roundtrip age_hours={age_h}"
                ),
            )

    if verdict is Verdict.DRIFT:
        summary = f"latest restore-verify FAILED ({age_txt}) — backup does not restore"
    elif verdict is Verdict.VERIFIED:
        summary = f"latest restore-verify passed ({age_txt}) — backup restores"
    else:
        summary = f"latest restore-verify is {verdict_str} ({age_txt})"
    return Finding(
        id=finding_id,
        physics=physics,
        verdict=verdict,
        summary=summary,
        detail=f"path={backup_dir} verify_result={verdict_str} age_hours={age_h}",
    )


def _probe_media_volume_index_declared(app: Any) -> Finding | None:
    """Probe 2e: a media-class volume MUST declare its library/index (design §14 (ii)/(iv)).

    The decided media policy is metadata-only-MANDATORY: a volume tagged ``backup_class: media``
    excludes its re-acquirable bytes BUT must capture the app's library/index DB, declared via the
    app-level ``backup_verify`` spec (``sqlite:<rel>`` | ``path:<rel>``). A media volume that
    declares NO index is "opted into a policy whose payload it failed to declare" — a recoverability
    gap (a media re-acquire would have no index to drive it), so it is **DRIFT**, never a silent
    pass. Returns None when the app has no media-class volume (not applicable).

    Reconciles the manifest's declared volumes against its declared verify spec — it does not assert
    a backup ran (that GROUND leg is ``_probe_backup_verify_result`` reading the real sidecar)."""
    media_vols = [
        v for v in getattr(app, "custom_volumes", []) if getattr(v, "backup_class", "") == "media"
    ]
    if not media_vols:
        return None  # no media-class volume — nothing to require

    app_key: str = getattr(app, "key", str(app))
    finding_id = f"recovery.media_index_declared.{app_key}"
    physics = f"media-class volume index declaration for app {app_key}"
    if not (getattr(app, "backup_verify", "") or "").strip():
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.DRIFT,
            summary=(
                f"media volume(s) declared but NO index — {len(media_vols)} media-class volume(s) "
                "exclude their bytes yet the app declares no backup_verify index to capture"
            ),
            detail=(
                f"media_volumes={[getattr(v, 'container_path', '?') for v in media_vols]} "
                "backup_verify=<empty> requires=sqlite:<rel>|path:<rel> (design §14)"
            ),
        )
    return Finding(
        id=finding_id,
        physics=physics,
        verdict=Verdict.VERIFIED,
        summary=f"media volume(s) declare an index ({app.backup_verify})",
        detail=f"media_volumes={len(media_vols)} backup_verify={app.backup_verify}",
    )


# Custom-class volume backups (#1287) are isolated under `<app_backup_dir>/volumes/<idx>/` so a
# volume's verify verdict cannot MASK the primary config-backup verdict (latest_verify_result reads
# ONE dir, newest sidecar wins). That isolation is also why a volume DRIFT needs its OWN probe:
# nothing else walks those subdirs. This literal mirrors cli/ms_backup._VOLUME_SUBDIR — the agent
# must NOT import the operational/CLI layer (two-owner firewall), so the contract is pinned equal by
# tests/test_recovery_audit_volume_verify.py::test_volume_subdir_contract instead.
_VOLUME_SUBDIR = "volumes"


def _probe_custom_volume_verify_results(app: Any, config_root: str | None = None) -> list[Finding]:
    """Probe 2f: per-volume restore-verify result for CONFIG-class custom volumes (#1292, #1281-class).

    #1287 landed the multi-volume execute loop (``cli/ms_backup.backup_custom_volumes``): each
    config-class custom volume is tar-backed + restore-verified + sidecar'd under
    ``<app_backup_dir>/volumes/<idx>/``, ISOLATED so a volume verdict can't mask the per-app
    config-backup verdict. But that isolation means a volume backup that DRIFTs (does not restore)
    wrote a sidecar **nothing surfaced** — the exact #1281-class silent-unverified gap. This probe
    closes it: it walks ``<app_backup_dir>/volumes/*/`` and emits a per-volume
    ``recovery.custom_volume_verify.<app_key>.<idx>`` finding, reading the SAME GROUND sidecar via
    :func:`latest_verify_result` (observe-only — never runs a restore; the agent firewall).

    Returns a LIST (zero-or-more findings — one per volume subdir carrying a verdict), unlike the
    single-finding probes. Empty when no backup dir resolves, no ``volumes/`` subdir exists, or no
    volume has a verify sidecar yet (the same no-noise contract as the per-app / platform probes:
    silence before a verify has run, never a silent green). Config-class volume tars are always LOCAL,
    so the off-host verify-scope tightening ``_probe_backup_verify_result`` applies does not apply here.
    """
    backup_dir = app_backup_dir(app, config_root)
    if not backup_dir:
        return []  # no backup dir resolvable — nothing to walk
    volumes_dir = Path(backup_dir) / _VOLUME_SUBDIR
    try:
        if not volumes_dir.is_dir():
            return []  # no custom-volume backups for this app
        subdirs = [d for d in volumes_dir.iterdir() if d.is_dir()]
    except OSError:
        return []  # unreadable — owned by no probe; stay silent (never a silent green)

    app_key: str = getattr(app, "key", str(app))

    # Deterministic order: numeric idx subdirs first (by value), then any others lexically.
    def _sort_key(d: Path) -> tuple[int, int | str]:
        return (0, int(d.name)) if d.name.isdigit() else (1, d.name)

    findings: list[Finding] = []
    for sub in sorted(subdirs, key=_sort_key):
        verdict_str, age_h = latest_verify_result(str(sub))
        if verdict_str is None:
            continue  # this volume has no restore-verify sidecar yet — silent (no noise)
        verdict = _VERIFY_VERDICT_MAP.get(verdict_str, Verdict.INDETERMINATE)
        age_txt = f"{age_h:.0f}h ago" if age_h is not None else "unknown age"
        idx = sub.name
        finding_id = f"recovery.custom_volume_verify.{app_key}.{idx}"
        physics = f"restore-verify result for custom volume[{idx}] of app {app_key}"
        if verdict is Verdict.DRIFT:
            summary = f"custom volume[{idx}] restore-verify FAILED ({age_txt}) — volume backup does not restore"
        elif verdict is Verdict.VERIFIED:
            summary = (
                f"custom volume[{idx}] restore-verify passed ({age_txt}) — volume backup restores"
            )
        else:
            summary = f"custom volume[{idx}] restore-verify is {verdict_str} ({age_txt})"
        findings.append(
            Finding(
                id=finding_id,
                physics=physics,
                verdict=verdict,
                summary=summary,
                detail=f"path={sub} verify_result={verdict_str} age_hours={age_h}",
            )
        )
    return findings


def _probe_platform_backup_verify_result(config_root: str | None = None) -> Finding | None:
    """Probe 2e: platform-DB restore-verify result — SYSTEM-LEVEL (#1281, #868 P2 residual).

    The per-app ``_probe_backup_verify_result`` iterates installed apps, so it never sees the SLOP
    platform DB's own backup, which ``ms-backup --platform`` writes to the FIXED
    ``<config_root>/backups/_platform`` dir (no app key). Its restore-verify verdict — including a
    DRIFT meaning "the platform DB backup does NOT restore" — was therefore written to a sidecar that
    nothing surfaced. This system-level probe closes that gap: it reads the SAME GROUND sidecar via
    :func:`backend.agent.backup.latest_verify_result`, observe-only (never runs a restore — the agent
    firewall), and emits a non-app finding. The platform DB backup is always LOCAL (a SQLite
    snapshot), so the off-host verify-scope tightening that ``_probe_backup_verify_result`` applies
    does not apply here.

    Returns None when config_root is unknown OR no platform verify has run yet (no sidecar) — the
    same no-noise contract as the per-app probe (silence, never a silent green).
    """
    finding_id = "recovery.platform_backup_verify"
    physics = "restore-verify result for the SLOP platform DB backup"

    backup_dir = platform_backup_dir(config_root)
    if not backup_dir:
        return None  # config_root unknown — nothing to resolve

    verdict_str, age_h = latest_verify_result(backup_dir)
    if verdict_str is None:
        return (
            None  # no platform restore-verify has run yet — silent until ms-backup --platform runs
        )

    verdict = _VERIFY_VERDICT_MAP.get(verdict_str, Verdict.INDETERMINATE)
    age_txt = f"{age_h:.0f}h ago" if age_h is not None else "unknown age"
    if verdict is Verdict.DRIFT:
        summary = f"latest platform-DB restore-verify FAILED ({age_txt}) — platform backup does not restore"
    elif verdict is Verdict.VERIFIED:
        summary = f"latest platform-DB restore-verify passed ({age_txt}) — platform backup restores"
    else:
        summary = f"latest platform-DB restore-verify is {verdict_str} ({age_txt})"
    return Finding(
        id=finding_id,
        physics=physics,
        verdict=verdict,
        summary=summary,
        detail=f"path={backup_dir} verify_result={verdict_str} age_hours={age_h}",
    )


# systemd unit name installed by installer/service.py::install_backup_timer (#868 P3). Kept in
# sync with installer/templates/ms-backup.timer.j2 — the timer that drives `ms-backup --all`.
_SCHEDULE_TIMER_UNIT = "ms-backup.timer"


def _probe_backup_schedule_overdue() -> Finding:
    """Probe 2d: scheduled-backup timer alive (#868 P3/§9 — system-level, not per-app).

    GROUND on the systemd timer that drives the scheduled `ms-backup --all` (installer §7). This is
    the dedicated red-signal for "the scheduler itself stopped", distinct from `_probe_backup_freshness`
    (which answers "is the EXISTING backup recent?" per app) — a disabled/failed timer is a
    recoverability gap BEFORE any backup goes stale. Observe-only: reads `systemctl is-active` and
    never starts/enables the timer (the agent runtime-only firewall).

    Verdicts: VERIFIED (timer active) · DRIFT (timer inactive/failed — scheduled backups not running)
    · INDETERMINATE (systemctl absent / not a systemd host / call failed — never a silent OK).
    """
    import subprocess  # local import — keeps the host dependency off the module-load path

    finding_id = "recovery.backup_schedule_overdue"
    physics = f"{_SCHEDULE_TIMER_UNIT} systemd unit active-state (systemctl is-active)"

    try:
        result = subprocess.run(
            ["systemctl", "is-active", _SCHEDULE_TIMER_UNIT],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except FileNotFoundError:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="schedule probe: systemctl not found (non-systemd host)",
            detail="",
        )
    except Exception as exc:  # timeout / OS error — ground truth unreachable, never silent OK
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="schedule probe: systemctl call failed",
            detail=f"{type(exc).__name__}: {exc}",
        )

    # `systemctl is-active` prints the state to stdout and exits non-zero when not active; read the
    # STATE word, not the exit code (mirrors host_audit._probe_clock_skew).
    state = result.stdout.strip() or "unknown"
    if state == "active":
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.VERIFIED,
            summary=f"{_SCHEDULE_TIMER_UNIT} active — scheduled backups running",
            detail=f"state={state}",
        )
    return Finding(
        id=finding_id,
        physics=physics,
        verdict=Verdict.DRIFT,
        summary=f"scheduled backups NOT running — {_SCHEDULE_TIMER_UNIT} is {state}",
        detail=f"state={state} — enable with `systemctl enable --now {_SCHEDULE_TIMER_UNIT}`",
    )
