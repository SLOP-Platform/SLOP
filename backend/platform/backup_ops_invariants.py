"""backend/platform/backup_ops_invariants.py — per-class restore invariants (#868 / #1302 split).

Extracted from ``backup_ops.py`` (the operational restore-verify tier) to keep that module under
the 500-line ``production_code`` cap. These are the **seam** the platform-DB integrity check + the
future per-app sentinels plug into without changing the verify core: each builds an
:data:`Invariant` — ``(scratch_dir) -> (ok, reason)`` — that
:func:`backend.platform.backup_ops.verify_backup_artifact` applies to a freshly-restored backup.

The three builders + the :data:`Invariant` alias are imported back into ``backup_ops`` and
re-exported, so every existing caller (``backup_ops.invariant_for`` / ``.sqlite_db_invariant`` /
``.path_present_invariant`` / the ``Invariant`` type) keeps resolving — a pure move, no behaviour
change.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

# A per-class restore invariant: given the scratch dir the artifact was restored INTO, return
# ``(ok, reason)``.  ``None`` (default) uses the generic "≥1 file restored" check. The SEAM the
# platform-DB integrity check + future per-app sentinels plug into without changing the core.
Invariant = Callable[[Path], "tuple[bool, str]"]


def sqlite_db_invariant(db_relpath: str) -> Invariant:
    """Build a per-class invariant that a restored SQLite DB (the SLOP platform DB, or any app's
    SQLite config) actually OPENS and passes ``PRAGMA integrity_check`` — the deep check the
    design (§2a/§4) calls for: a tar that extracts but yields a corrupt DB is still a bad backup.

    Returns an :data:`Invariant` for :func:`verify_backup_artifact`: it locates ``db_relpath``
    under the restored scratch dir and reports ``(ok, reason)``.  Read-only open; never mutates
    the restored copy.  (Schema-head/version assertion is the P1b refinement — out of this seam's
    scope, which is "is this a recoverable database".)
    """

    def _check(scratch: Path) -> tuple[bool, str]:
        db = scratch / db_relpath
        if not db.is_file():
            return False, f"expected DB {db_relpath!r} not present in restored backup"
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
            try:
                row = con.execute("PRAGMA integrity_check").fetchone()
            finally:
                con.close()
        except sqlite3.Error as exc:
            return False, f"restored DB {db_relpath!r} will not open: {exc}"
        if not row or row[0] != "ok":
            return False, f"restored DB {db_relpath!r} failed integrity_check: {row}"
        return True, f"{db_relpath!r} opens + integrity_check ok"

    return _check


def path_present_invariant(relpath: str) -> Invariant:
    """Build an invariant asserting the restored backup contains *relpath* (a declared sentinel).

    The lightest per-app check: not "is this DB healthy" but "did the restore actually contain the
    file the operator declared as proof-of-completeness". A falsy result is ``DRIFT`` in
    :func:`verify_backup_artifact` (restored, but the sentinel is missing → an incomplete backup).
    """

    def _check(scratch: Path) -> tuple[bool, str]:
        target = scratch / relpath
        if target.exists():
            return True, f"sentinel {relpath!r} present in restored backup"
        return False, f"declared sentinel {relpath!r} missing from restored backup"

    return _check


def invariant_for(backup_verify: str) -> Invariant | None:
    """Map a manifest ``backup_verify`` spec to a per-class restore invariant (the seam plug-point).

    Specs (docs/BACKUP-PRODUCT-868-DESIGN.md §4): ``"sqlite:<rel>"`` → :func:`sqlite_db_invariant`;
    ``"path:<rel>"`` → :func:`path_present_invariant`. An empty or unrecognised spec returns
    ``None`` → :func:`verify_backup_artifact` applies the generic "≥1 file restored" invariant
    (never a silent skip of verify — an unknown spec degrades to the generic check, it does not
    disable it).
    """
    spec = (backup_verify or "").strip()
    if spec.startswith("sqlite:"):
        rel = spec[len("sqlite:") :].strip()
        return sqlite_db_invariant(rel) if rel else None
    if spec.startswith("path:"):
        rel = spec[len("path:") :].strip()
        return path_present_invariant(rel) if rel else None
    return None
