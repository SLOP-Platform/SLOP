"""backend/platform/backup_ops.py — operational backup restore-verify (#868 P1 keystone).

This is the **operational tier** of the backup product (the layer the future ``ms-backup``
CLI wraps), deliberately OUTSIDE ``backend/agent/`` — the SLOP agent is runtime-only /
observe-only (two-owner firewall, CLAUDE.md "Knowledge-Lifecycle"): it READS a verify
result, it never RUNS a restore. Tar EXTRACTION is an action, so it lives here, not in the
agent. ``backend/agent/backup.py`` only reads the sidecar this module writes.

The keystone of the backup product (docs/BACKUP-PRODUCT-868-DESIGN.md §4): a backup that has
never been restored is stored-and-trusted theater. ``verify_backup_artifact`` restores an
artifact to a SCRATCH dir (never over live data) and reports the pinned verdict vocabulary:

  * ``verified``      — the artifact extracted and the invariant held (GROUND: it round-trips).
  * ``DRIFT``         — extraction failed / empty (the backup is bad — the highest-value signal).
  * ``INDETERMINATE`` — the artifact or scratch space was unreachable (LOUD, never a silent OK).

``write_verify_sidecar`` records the verdict next to the artifact via a ``.tmp``→atomic-rename
(same partial-write discipline as the backups themselves, §3), so a crash mid-write never
leaves a half-written verdict the agent would misread.
"""

from __future__ import annotations

import json
import dataclasses
import os
import sqlite3
import tarfile
import tempfile
import time
from pathlib import Path

from backend.core.logging import get_logger

# Per-class restore invariants (the seam verify_backup_artifact applies) live in a sibling module
# to keep this file under the production_code cap (#1302). Re-exported below so existing callers —
# backup_ops.invariant_for / .sqlite_db_invariant / .path_present_invariant / the Invariant type —
# keep resolving unchanged.
from backend.platform.backup_ops_invariants import (
    Invariant,
    invariant_for,
    path_present_invariant,
    sqlite_db_invariant,
)

log = get_logger(__name__)

# Sidecar naming: a verify result for ``foo.tar.gz`` is ``.verify-foo.tar.gz.json``. The leading
# dot keeps it out of the artifact listing that drives freshness/age, so a sidecar is never
# mistaken for a backup (freshness counts files; readers filter by this prefix). Design §4.
VERIFY_SIDECAR_PREFIX = ".verify-"
VERIFY_SIDECAR_SUFFIX = ".json"

VERDICT_VERIFIED = "verified"
VERDICT_DRIFT = "DRIFT"
VERDICT_INDETERMINATE = "INDETERMINATE"

# Where the platform DB's backups live, relative to the resolved config_root
# (``<config_root>/backups/_platform``). The leading underscore keeps it out of any per-app key
# namespace (no app may be named "_platform"). SoT here in the module that OWNS platform backup
# verify/sidecar; cli/ms_backup.py and the recovery probe (recovery_probes/backup.py, #1281) both
# import it so the two sides of the contract can never drift to different directories.
PLATFORM_BACKUP_SUBDIR = "_platform"

# verify_scope (design §13 SIDECAR CONTRACT): WHAT a sidecar's verdict proved — local-plaintext
# (the local tar round-trips) vs offhost-ciphertext-roundtrip (the off-host ciphertext decrypted
# in-window + round-trips, see backup_offhost). Older sidecars LACK the field ⇒ readers treat a
# MISSING scope as local-plaintext (conservative / RED-for-offhost). The recovery probe refuses
# VERIFIED for an off-host-configured app whose latest scope is only local-plaintext.
VERIFY_SCOPE_LOCAL = "local-plaintext"
VERIFY_SCOPE_OFFHOST = "offhost-ciphertext-roundtrip"


# ── Off-host target seam (#868 P4 / design §13 [DToC-3]) ─────────────────────────
# The OPEN protocol set is encoded by the rclone remote-NAME, not by enum members (parameterize
# the interface, don't enumerate the ssh/s3 members you won't ship). SLOP stores ONLY the remote-
# name string; the operator provisions ``rclone.conf`` (with its secret) out-of-band and SLOP NEVER
# writes that secret. The off-host execute engine consuming this seam lives in ``backup_offhost.py``.
BACKUP_TARGET_KINDS = ("local", "rclone")

# The ONLY keys a target spec may carry. The credential firewall is ALLOWLIST-by-construction
# (design §13 / #1253 "never sanitize/denylist"): any other key (secret-bearing or not, known or
# not-yet-invented) is rejected fail-closed, so SLOP never stores the rclone secret. A denylist
# would rot the moment a new secret-key name appears.
_ALLOWED_SPEC_KEYS = frozenset({"kind", "remote"})

# Chars that mean `remote` is an inline rclone CONFIG (`s3,access_key=…`), not a bare remote-NAME
# — a second credential-smuggle path past the key allowlist.
_REMOTE_NAME_FORBIDDEN = (" ", "\t", ",", "=", "\n")


@dataclasses.dataclass(frozen=True)
class BackupTarget:
    """Where a backup artifact is stored. ``kind`` is one of :data:`BACKUP_TARGET_KINDS`;
    ``remote`` is the rclone remote-NAME (a string, never a secret) — required for
    ``rclone``, empty for ``local``. Construct via :func:`parse_backup_target` so the
    credential-firewall + phantom-enum validators always run."""

    kind: str
    remote: str = ""


LOCAL_TARGET = BackupTarget(kind="local")


def parse_backup_target(spec: str | dict[str, object] | BackupTarget | None) -> BackupTarget:
    """Validate + build a :class:`BackupTarget` from a CLI string or a config dict.

    Accepted forms: ``None``/``""``/``"local"`` → local; ``"rclone:<remote>"`` or
    ``{"kind": "rclone", "remote": "<name>"}`` → off-host via the named rclone remote.

    Enforces the design §13 firewall (each path proven-red in tests): keys ALLOWLISTED to
    ``{kind, remote}`` (any other key rejected fail-closed → SLOP never stores the rclone secret);
    ``kind`` in :data:`BACKUP_TARGET_KINDS` (``ssh``/``s3`` are phantom enum members → raise);
    ``remote`` a bare remote-NAME (an inline rclone config raises); ``rclone`` requires a non-empty
    ``remote`` and ``local`` must not carry one. Raises ``ValueError`` on any violation — never a
    silent local fallback (the "green-local but no off-host copy" theater §5 forbids)."""
    if isinstance(spec, BackupTarget):
        # Re-validate even a pre-built target — parse is the only construction contract.
        spec = {"kind": spec.kind, "remote": spec.remote}
    if spec is None or spec == "":
        return LOCAL_TARGET
    if isinstance(spec, str):
        kind_part, _, remote_part = spec.partition(":")
        spec = {"kind": kind_part.strip(), "remote": remote_part.strip()}
    if not isinstance(spec, dict):
        raise ValueError(f"backup target spec must be str or dict, got {type(spec).__name__}")
    extra = {str(k) for k in spec} - _ALLOWED_SPEC_KEYS
    if extra:
        raise ValueError(
            f"backup target accepts only {sorted(_ALLOWED_SPEC_KEYS)} — rejected key(s) "
            f"{sorted(extra)}: SLOP stores only the rclone remote-name, never a secret/"
            "credential (provision rclone.conf out-of-band, design §13 [DToC-3])"
        )
    kind = str(spec.get("kind", "local")).strip() or "local"
    remote = str(spec.get("remote", "")).strip()
    if kind not in BACKUP_TARGET_KINDS:
        raise ValueError(
            f"unknown backup target kind {kind!r} — must be one of {BACKUP_TARGET_KINDS} "
            "(ssh/s3 are not enum members; use kind='rclone' with the remote-name)"
        )
    if remote and any(c in remote for c in _REMOTE_NAME_FORBIDDEN):
        raise ValueError(
            f"rclone remote {remote!r} must be a bare remote-NAME (e.g. 'offsite-b2'), not an "
            "inline config — credentials live in operator-provisioned rclone.conf (design §13)"
        )
    if kind == "rclone" and not remote:
        raise ValueError("rclone backup target requires a non-empty remote name")
    if kind == "local" and remote:
        raise ValueError("local backup target must not carry a remote name")
    return BackupTarget(kind=kind, remote=remote)


def sidecar_path(backup_dir: str | Path, artifact_name: str) -> Path:
    """Path of the verify-result sidecar for *artifact_name* under *backup_dir*."""
    return Path(backup_dir) / f"{VERIFY_SIDECAR_PREFIX}{artifact_name}{VERIFY_SIDECAR_SUFFIX}"


def verify_backup_artifact(
    artifact: str | Path,
    scratch_dir: str | Path | None = None,
    invariant: Invariant | None = None,
) -> tuple[str, str]:
    """Restore *artifact* to a scratch dir and report whether it is recoverable.

    GROUND: actually extracts the tar (never over live data — into a throwaway scratch dir
    that is removed afterwards). Returns ``(verdict, reason)`` using the pinned vocabulary.

    A ``DRIFT`` here means "you thought you had a backup; you do not" — the single most
    valuable thing the product emits. Never raises: an unreadable artifact or unusable scratch
    space is ``INDETERMINATE`` (LOUD), a corrupt/truncated/empty archive is ``DRIFT``.

    *invariant* is the per-class deep check (the SEAM for the platform-DB integrity check —
    ``verify_sqlite_db`` — and future per-app sentinels).  When ``None`` the generic invariant
    applies: the restore must yield ≥1 real file.  When provided, it runs AFTER a successful
    extract over the scratch dir and a falsy result is ``DRIFT`` (the artifact restored but its
    content failed the class invariant — e.g. a config DB that extracts but won't open).

    Tar extraction uses the ``data`` filter (Python's safe-extraction policy) so a malicious or
    malformed archive cannot escape the scratch dir via path traversal / absolute paths.
    """
    art = Path(artifact)
    try:
        if not art.is_file():
            return VERDICT_INDETERMINATE, f"artifact not found: {art}"
    except OSError as exc:
        return VERDICT_INDETERMINATE, f"cannot stat artifact {art}: {exc}"

    # Scratch: caller-provided (tests) or a fresh temp dir we own and remove.
    own_scratch = scratch_dir is None
    try:
        scratch = (
            Path(scratch_dir)
            if scratch_dir is not None
            else Path(tempfile.mkdtemp(prefix="ms-backup-verify-"))
        )
    except OSError as exc:
        return VERDICT_INDETERMINATE, f"scratch space unavailable: {exc}"

    try:
        try:
            with tarfile.open(art, "r:*") as tf:
                # filter="data" rejects absolute paths, ``..`` traversal, and unsafe links.
                tf.extractall(scratch, filter="data")
        except (tarfile.TarError, EOFError, OSError) as exc:
            # A truncated/corrupt/non-tar artifact lands here — the backup is unrecoverable.
            return VERDICT_DRIFT, f"restore FAILED (artifact is not a recoverable archive): {exc}"

        # Generic invariant: the restore must yield at least one real file. An empty archive
        # extracts "successfully" yet restores nothing — that is a bad backup, not a good one.
        restored = [p for p in scratch.rglob("*") if p.is_file()]
        if not restored:
            return VERDICT_DRIFT, "restore produced no files — empty/contentless backup"
        # Per-class deep invariant (platform-DB integrity / per-app sentinel), if supplied.
        if invariant is not None:
            try:
                ok, reason = invariant(scratch)
            except Exception as exc:
                return VERDICT_DRIFT, f"restore invariant raised (treated as unrecoverable): {exc}"
            if not ok:
                return VERDICT_DRIFT, f"restored but invariant FAILED: {reason}"
            return VERDICT_VERIFIED, f"restored {len(restored)} file(s); invariant OK: {reason}"
        return VERDICT_VERIFIED, f"restored {len(restored)} file(s) to scratch — recoverable"
    finally:
        if own_scratch:
            _rmtree_quiet(scratch)


def snapshot_sqlite(db_path: str | Path, dest_path: str | Path) -> None:
    """Write a CONSISTENT snapshot of a (possibly live) SQLite DB at *db_path* to *dest_path*.

    Uses SQLite's online backup API (``Connection.backup``) rather than copying the file: a live
    DB has -wal/-shm sidecars and a plain ``cp`` can capture a torn page. The backup API produces
    a single self-consistent file even while the source is being written. GROUND: the snapshot is
    a real recoverable DB (the §2a integrity invariant verifies it after restore). Raises
    ``sqlite3.Error``/``OSError`` on failure — the caller (CLI) reports it as an error, never a
    silent success.
    """
    src = sqlite3.connect(f"file:{Path(db_path)}?mode=ro", uri=True)
    try:
        dst = sqlite3.connect(str(dest_path))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()


def execute_backup(
    source: str | Path,
    backup_dir: str | Path,
    name_prefix: str,
    timestamp: str | None = None,
    target: BackupTarget = LOCAL_TARGET,
) -> Path:
    """Create a ``.tar.gz`` backup of *source* (a file or directory) under *backup_dir*.

    The archive is named ``<name_prefix>-<timestamp>.tar.gz`` and the source is stored under its
    own basename as the arcname, so a restore reproduces ``<basename>/...`` (or the bare file) —
    the stable relpath the per-class verify invariant keys on (e.g. ``sqlite_db_invariant`` looks
    for ``state.db`` when the snapshot's basename is ``state.db``).

    Written to ``<artifact>.tmp`` and **atomically renamed** on success only (the §3 partial-write
    discipline): an interrupted backup leaves a ``.tmp`` that freshness/verify ignore, never a
    half-written artifact mistaken for a good one. GROUND: actually tars the bytes. Returns the
    final artifact path. Raises ``OSError``/``tarfile.TarError`` on an unreadable source or
    unwritable dir — the CLI converts that to a loud error, never a claimed success.
    """
    if target.kind != "local":
        # Honest refusal — NOT a silent local-only store. ``execute_backup`` is the pure-local tar
        # primitive; the off-host flow lives in ``backup_offhost.execute_offhost_backup`` (kept
        # separate so the external-binary machinery doesn't crowd this cap + stays cycle-free).
        raise NotImplementedError(
            f"off-host backup target {target.kind!r} (remote {target.remote!r}) is not handled by "
            "execute_backup (the local primitive) — call backend.platform.backup_offhost."
            "execute_offhost_backup (design §13); refusing to silently store locally"
        )
    src = Path(source)
    bdir = Path(backup_dir)
    bdir.mkdir(parents=True, exist_ok=True)
    ts = timestamp if timestamp is not None else time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    dest = bdir / f"{name_prefix}-{ts}.tar.gz"
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    try:
        with tarfile.open(tmp, "w:gz") as tf:
            tf.add(src, arcname=src.name)
        os.replace(tmp, dest)  # atomic on POSIX — the commit point
    except (OSError, tarfile.TarError):
        # Clean up the partial .tmp so it can never be read as a backup.
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return dest


def write_verify_sidecar(
    backup_dir: str | Path,
    artifact_name: str,
    verdict: str,
    verify_scope: str = VERIFY_SCOPE_LOCAL,
) -> Path:
    """Atomically record *verdict* for *artifact_name* under *backup_dir*; return the sidecar path.

    ``.tmp``→rename so a crash mid-write never leaves a half-written verdict (the agent reader
    would otherwise see a partial JSON and mis-advise). The recorded payload is GROUND-readable.
    *verify_scope* (design §13) records WHAT the verdict proved — :data:`VERIFY_SCOPE_LOCAL`
    (default) or :data:`VERIFY_SCOPE_OFFHOST`; the recovery probe refuses VERIFIED for an off-host-
    configured app whose latest scope is only local-plaintext.
    """
    dest = sidecar_path(backup_dir, artifact_name)
    payload = {
        "result": verdict,
        "ts": time.time(),
        "artifact": artifact_name,
        "verify_scope": verify_scope,
    }
    tmp = dest.with_suffix(dest.suffix + ".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    os.replace(tmp, dest)  # atomic on POSIX
    return dest


def read_verify_verdict(backup_dir: str | Path, artifact_name: str) -> str | None:
    """Read the recorded restore-verify verdict for *artifact_name*, or ``None`` if none/unreadable.

    GROUND: reads the ``.verify-<artifact>.json`` sidecar :func:`write_verify_sidecar` wrote. Used
    by :func:`prune_backups` to gate destructive deletion on a per-artifact verdict (not a global
    "some verify ran" signal). Never raises.
    """
    sc = sidecar_path(backup_dir, artifact_name)
    try:
        return str(json.loads(sc.read_text(encoding="utf-8"))["result"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _is_artifact(p: Path) -> bool:
    """A real backup artifact this module created — a ``.tar.gz`` file.

    Deliberately STRICTER than the freshness probe's filter: ``prune_backups`` DELETES, so it must
    only ever consider files it KNOWS are our backups (``execute_backup`` always writes ``.tar.gz``).
    This excludes ``.verify-*.json`` sidecars, ``.tmp`` partials, AND any foreign file an operator
    left in the dir — a destructive op must never touch something that is not ours (review hardening).
    """
    return p.is_file() and p.name.endswith(".tar.gz")


def prune_backups(backup_dir: str | Path, keep: int = 7) -> list[Path]:
    """Fail-safe retention prune (docs/BACKUP-PRODUCT-868-DESIGN.md §3). Returns the deleted artifacts.

    Keeps the ``keep`` newest artifacts UNCONDITIONALLY, then deletes older ones **only when a
    NEWER restore-verified backup exists** — the prune gate is *recoverability*, never mere
    time-elapsed. The safety invariants (all GROUND, all proven-red):

      * never delete if there are ``<= keep`` artifacts (covers the only/newest-backup case);
      * never delete the newest artifact, ever;
      * never delete ANYTHING unless at least one artifact is restore-``verified`` (no verified
        anchor ⇒ keep everything — you must not prune down to backups you never proved restorable);
      * only delete an artifact OLDER than the newest verified one (so a vanished/failed newer
        backup can never cause an older good one to be pruned).

    Each deleted artifact's ``.verify-*.json`` sidecar is removed with it. ``keep < 1`` is treated
    as ``1`` (never prune to zero). Never raises on an individual unlink (best-effort, logged).
    """
    keep = max(1, keep)
    bdir = Path(backup_dir)
    try:
        artifacts = sorted(
            (a for a in bdir.iterdir() if _is_artifact(a)),
            key=lambda a: a.stat().st_mtime,
            reverse=True,  # newest first
        )
    except OSError as exc:
        log.debug("prune_backups: cannot read %s: %s", bdir, exc)
        return []
    if len(artifacts) <= keep:
        return []
    # The newest restore-verified artifact's mtime is the prune anchor (a newer verified backup
    # must exist before an older one may go). No verified anchor ⇒ prune nothing.
    verified_mtimes = [
        a.stat().st_mtime
        for a in artifacts
        if read_verify_verdict(bdir, a.name) == VERDICT_VERIFIED
    ]
    if not verified_mtimes:
        return []
    anchor = max(verified_mtimes)
    deleted: list[Path] = []
    for a in artifacts[keep:]:  # candidates strictly outside the keep window (the oldest ones)
        if a.stat().st_mtime < anchor:  # a newer restore-verified backup exists → safe to drop
            try:
                a.unlink()
            except OSError as exc:
                log.debug("prune_backups: could not remove %s: %s", a, exc)
                continue  # artifact still present → NOT deleted; leave its sidecar too
            # Artifact is gone — record it BEFORE the sidecar cleanup so a sidecar-unlink failure
            # can never un-record a real deletion (review MAJOR: orphan-on-unlink). Sidecar removal
            # is best-effort; a leftover sidecar is informational, not corruption.
            deleted.append(a)
            try:
                sidecar_path(bdir, a.name).unlink(missing_ok=True)
            except OSError as exc:
                log.debug("prune_backups: orphaned sidecar for %s: %s", a, exc)
    return deleted


def _rmtree_quiet(path: Path) -> None:
    """Best-effort recursive remove of a scratch dir; never raises."""
    try:
        for child in sorted(path.rglob("*"), reverse=True):
            try:
                child.unlink() if child.is_file() or child.is_symlink() else child.rmdir()
            except OSError:
                pass
        path.rmdir()
    except OSError as exc:
        log.debug("scratch cleanup incomplete for %s: %s", path, exc)


__all__ = [
    "BACKUP_TARGET_KINDS",
    "LOCAL_TARGET",
    "PLATFORM_BACKUP_SUBDIR",
    "VERDICT_DRIFT",
    "VERDICT_INDETERMINATE",
    "VERDICT_VERIFIED",
    "VERIFY_SCOPE_LOCAL",
    "VERIFY_SCOPE_OFFHOST",
    "VERIFY_SIDECAR_PREFIX",
    "VERIFY_SIDECAR_SUFFIX",
    "BackupTarget",
    "Invariant",
    "execute_backup",
    "invariant_for",
    "parse_backup_target",
    "path_present_invariant",
    "prune_backups",
    "read_verify_verdict",
    "sidecar_path",
    "snapshot_sqlite",
    "sqlite_db_invariant",
    "verify_backup_artifact",
    "write_verify_sidecar",
]
