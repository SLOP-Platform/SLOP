"""backend/core/migrations.py

SQLite migration runner for SLOP v4.

Design and rationale: docs/cleanup/01_migrations_design.md
Tool decision (custom vs Alembic): docs/adr/0001-database-migrations.md

The runner is invoked from `init_db()` on every startup. It is idempotent:
calling it twice in a row is a no-op the second time. It applies all
pending migrations in `migrations/` in numeric order, recording each in
the `schema_migrations` table.

Public API:
    run_migrations(db_path, migrations_dir=None, *, backup=True) -> MigrationResult

Errors raised:
    MigrationLockError       - another process is currently applying migrations
    MigrationChecksumError   - a previously-applied migration file has been edited
    MigrationError           - a migration failed; backup path included in message

The runner takes a hot-copy backup of state.db before applying any pending
migrations (controllable via backup=). Backups are kept for the most recent
5 batches; older ones are pruned.
"""

from __future__ import annotations

import hashlib
import importlib.util
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from backend.core.logging import get_logger

log = get_logger(__name__)

# Module-resolved default migrations directory: <repo_root>/migrations
_DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"

# How many pre-migration backups to retain. Older ones are pruned.
_BACKUP_RETENTION = 5

# Filename pattern: NNN_anything.sql or NNN_anything.py — three-digit prefix.
_MIGRATION_NAME_RE = re.compile(r"^(\d{3})_[a-z0-9_]+\.(sql|py)$")

_CREATE_SCHEMA_MIGRATIONS = """\
CREATE TABLE IF NOT EXISTS schema_migrations (
    version     INTEGER PRIMARY KEY,
    filename    TEXT    NOT NULL,
    checksum    TEXT    NOT NULL,
    applied_at  INTEGER NOT NULL DEFAULT (unixepoch()),
    duration_ms INTEGER NOT NULL DEFAULT 0
)
"""


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class MigrationError(Exception):
    """A migration failed. Message includes backup restore instructions."""


class MigrationLockError(MigrationError):
    """Another process holds the migration lock."""


class MigrationChecksumError(MigrationError):
    """A migration file on disk does not match the checksum recorded
    in schema_migrations. Migrations are immutable after merge."""


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class MigrationResult:
    """Outcome of a run_migrations() call."""

    applied: list[int] = field(default_factory=list)
    stamped_baseline: bool = False
    backup_path: Path | None = None
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


@dataclass
class _MigrationFile:
    version: int
    filename: str
    path: Path
    content: bytes

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.content).hexdigest()

    @property
    def is_sql(self) -> bool:
        return self.path.suffix == ".sql"


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ensure_schema_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(_CREATE_SCHEMA_MIGRATIONS)
    conn.commit()


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _applied_versions(conn: sqlite3.Connection) -> dict[int, tuple[str, str]]:
    """Return {version: (filename, checksum)} from schema_migrations."""
    rows = conn.execute("SELECT version, filename, checksum FROM schema_migrations").fetchall()
    return {r[0]: (r[1], r[2]) for r in rows}


def _take_backup(db_path: Path) -> Path:
    """Hot-copy state.db to state.db.bak.<unix-timestamp>."""
    timestamp = int(time.time())
    backup_path = db_path.with_name(f"{db_path.name}.bak.{timestamp}")
    src = sqlite3.connect(db_path)
    try:
        dst = sqlite3.connect(backup_path)
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    log.info("Pre-migration backup taken: %s", backup_path)
    return backup_path


def _prune_backups(db_path: Path, retain: int = _BACKUP_RETENTION) -> None:
    """Delete all but the `retain` most-recent backups."""
    pattern = f"{db_path.name}.bak.*"
    backups = sorted(
        db_path.parent.glob(pattern),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for old in backups[retain:]:
        try:
            old.unlink()
            log.debug("Pruned old migration backup: %s", old.name)
        except OSError as e:
            log.warning("Could not prune %s: %s", old, e)


def _scan_migrations(migrations_dir: Path) -> list[_MigrationFile]:
    """Scan migrations_dir for NNN_*.{sql,py} files (ignores _legacy/ and
    any filename starting with _). Returns sorted list."""
    found: list[_MigrationFile] = []
    for path in migrations_dir.iterdir():
        if path.is_dir():
            continue
        m = _MIGRATION_NAME_RE.match(path.name)
        if not m:
            continue
        version = int(m.group(1))
        content = path.read_bytes()
        found.append(
            _MigrationFile(
                version=version,
                filename=path.name,
                path=path,
                content=content,
            )
        )
    found.sort(key=lambda f: f.version)
    # Validate: no duplicate versions
    seen: dict[int, str] = {}
    for mf in found:
        if mf.version in seen:
            raise MigrationError(
                f"Duplicate migration version {mf.version:03d}: "
                f"{seen[mf.version]} and {mf.filename}"
            )
        seen[mf.version] = mf.filename
    return found


def _apply_sql_migration(conn: sqlite3.Connection, mig: _MigrationFile) -> None:
    """Run a .sql migration. executescript() handles any BEGIN/COMMIT inside.

    Idempotency: ALTER TABLE … ADD COLUMN statements that fail with
    "duplicate column name" are silently skipped — the column already
    exists, which is the desired post-migration state.
    """
    sql = mig.content.decode("utf-8")
    try:
        conn.executescript(sql)
    except sqlite3.OperationalError as exc:
        if "duplicate column name" in str(exc).lower():
            # Column already present — migration is effectively a no-op.
            # This happens when schema.sql is applied directly (v3 installs)
            # and the migration only adds a column that schema.sql already has.
            log.info(
                "Migration %s: column already exists (idempotent skip): %s",
                mig.filename,
                exc,
            )
        else:
            raise


def _apply_python_migration(conn: sqlite3.Connection, mig: _MigrationFile) -> None:
    """Import the .py migration module and call upgrade(conn)."""
    spec = importlib.util.spec_from_file_location(
        f"_slop_migration_{mig.version:03d}",
        str(mig.path),
    )
    if spec is None or spec.loader is None:
        raise MigrationError(
            f"Could not load migration module {mig.filename}. Check the file is valid Python."
        )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    upgrade = getattr(module, "upgrade", None)
    if not callable(upgrade):
        raise MigrationError(
            f"Migration {mig.filename} must define a callable "
            "`def upgrade(conn: sqlite3.Connection) -> None`."
        )
    upgrade(conn)


def _stamp_baseline(conn: sqlite3.Connection, baseline: _MigrationFile) -> None:
    """Record migration 001 as applied without re-running it (v3 DB path)."""
    conn.execute(
        "INSERT OR IGNORE INTO schema_migrations "
        "(version, filename, checksum, applied_at, duration_ms) "
        "VALUES (?, ?, ?, unixepoch(), 0)",
        (baseline.version, baseline.filename, baseline.checksum),
    )
    conn.commit()
    log.info(
        "Stamped baseline migration %03d (%s) on existing DB",
        baseline.version,
        baseline.filename,
    )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_migrations(
    db_path: Path,
    migrations_dir: Path | None = None,
    *,
    backup: bool = True,
) -> MigrationResult:
    """Apply all pending migrations to db_path.

    Idempotent: safe to call on every startup.

    Raises:
        MigrationLockError      if another process is migrating right now
        MigrationChecksumError  if a committed migration file was edited
        MigrationError          if a migration fails (with backup restore path)
    """
    t_start = time.monotonic()
    result = MigrationResult()

    if migrations_dir is None:
        migrations_dir = _DEFAULT_MIGRATIONS_DIR

    try:
        return _run_migrations_inner(db_path, migrations_dir, backup, t_start, result)
    except MigrationError:
        raise
    except sqlite3.OperationalError as exc:
        if "locked" in str(exc).lower():
            raise MigrationLockError(
                "Another process is currently applying migrations (database is locked). "
                f"Wait a moment and retry. DB: {db_path}"
            ) from exc
        raise


def _apply_one_migration(
    mig: _MigrationFile,
    db_path: Path,
    result: MigrationResult,
) -> None:
    """Apply one migration file in its own connection + transaction.

    Records success in `result.applied`. On failure, rolls back and
    raises MigrationError with a restore hint pointing at the backup
    (if one was taken).
    """
    t_mig_start = time.monotonic()
    log.info("Applying migration %03d: %s", mig.version, mig.filename)

    mig_conn = sqlite3.connect(db_path)
    mig_conn.row_factory = sqlite3.Row
    try:
        if mig.is_sql:
            _apply_sql_migration(mig_conn, mig)
        else:
            mig_conn.execute("BEGIN IMMEDIATE")
            _apply_python_migration(mig_conn, mig)

        duration_ms = int((time.monotonic() - t_mig_start) * 1000)

        # Record the applied migration inside the same connection
        mig_conn.execute(
            "INSERT OR REPLACE INTO schema_migrations "
            "(version, filename, checksum, applied_at, duration_ms) "
            "VALUES (?, ?, ?, unixepoch(), ?)",
            (mig.version, mig.filename, mig.checksum, duration_ms),
        )
        mig_conn.commit()
        result.applied.append(mig.version)
        log.info("Migration %03d applied in %d ms", mig.version, duration_ms)
    except Exception as exc:
        try:
            mig_conn.rollback()
        except Exception:  # noqa: S110  # best-effort rollback before re-raising MigrationError
            pass
        restore_cmd = (
            f"cp {result.backup_path} {db_path}"
            if result.backup_path
            else "no backup taken (backup=False)"
        )
        raise MigrationError(
            f"Migration {mig.filename} failed: {exc}\n"
            f"Backup: {result.backup_path}\n"
            f"Restore command: {restore_cmd}"
        ) from exc
    finally:
        mig_conn.close()


def _run_migrations_inner(
    db_path: Path,
    migrations_dir: Path,
    backup: bool,
    t_start: float,
    result: MigrationResult,
) -> MigrationResult:

    # --- 1. Ensure schema_migrations exists; acquire advisory lock --------
    lock_conn = sqlite3.connect(db_path, timeout=0)
    try:
        _ensure_schema_migrations(lock_conn)
        try:
            lock_conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as exc:
            lock_conn.close()
            raise MigrationLockError(
                "Another process is currently applying migrations. "
                "Wait a moment and retry. If this persists, check for a "
                f"stuck migration process on {db_path}."
            ) from exc

        applied = _applied_versions(lock_conn)
        is_fresh = len(applied) == 0
        platform_exists = _table_exists(lock_conn, "platform")

        # Detect v3 DB: platform table exists, no schema_migrations rows
        is_v3_db = is_fresh and platform_exists

        lock_conn.commit()
    finally:
        lock_conn.close()

    # --- 2. Scan migrations directory ------------------------------------
    all_migrations = _scan_migrations(migrations_dir)
    if not all_migrations:
        log.info("No migration files found in %s", migrations_dir)
        return result

    baseline = all_migrations[0]  # always migration 001

    # --- 3. Stamp baseline on v3 DBs (don't re-run 001) -----------------
    # Stamping only applies when: the DB is a v3 install (platform table exists,
    # no schema_migrations rows) AND the migrations directory contains version 001.
    # If there's no version-001 file, there's nothing to stamp; proceed normally.
    if is_v3_db and baseline.version == 1:
        stamp_conn = sqlite3.connect(db_path)
        try:
            _ensure_schema_migrations(stamp_conn)
            _stamp_baseline(stamp_conn, baseline)
            applied[baseline.version] = (baseline.filename, baseline.checksum)
        finally:
            stamp_conn.close()
        result.stamped_baseline = True

    # --- 4. Validate checksums of already-applied migrations -------------
    # The baseline (genesis) migration is a REGENERABLE snapshot, not an
    # immutable incremental: it is legitimately re-cut (e.g. 1e60af2 rewrote
    # example-domain *comments* in 001_baseline.sql — "the cleaned baseline as
    # genesis, no historical checksum to preserve, per operator"). An existing
    # install that recorded the OLD baseline checksum must still upgrade
    # cleanly, so the baseline is EXEMPT from the immutability check (#880
    # deliverable-2 / #1052 root-cause-2: validating it raised
    # MigrationChecksumError on every post-1e60af2 upgrade-from-old-baseline).
    # `baseline` is `all_migrations[0]` (the lowest-versioned/genesis file —
    # always 001 in this repo), so the predicate exempts *the genesis itself*,
    # not a hardcoded number: if the genesis ever shipped as a higher version
    # that higher version is the snapshot to exempt. Only incremental migrations
    # (v2+) carry the immutable-after-merge guarantee; their schema corrections
    # apply on top of any baseline variant.
    # ASSUMPTION (load-bearing): a baseline re-cut is comment/non-schema only —
    # a SCHEMA-changing edit belongs in an incremental migration, never in the
    # baseline, else exempting it would silently mask a divergent starting
    # schema on old-baseline DBs (#880 review caveat; follow-up: schema-sync gate).
    for version, (filename, recorded_checksum) in applied.items():
        if version == baseline.version:
            continue
        mig_file = next((m for m in all_migrations if m.version == version), None)
        if mig_file is None:
            # File was deleted — log a warning but don't hard-fail
            log.warning(
                "Migration %03d (%s) is in schema_migrations but not on disk",
                version,
                filename,
            )
            continue
        actual_checksum = mig_file.checksum
        if actual_checksum != recorded_checksum:
            raise MigrationChecksumError(
                f"Migration file {mig_file.filename} has been modified since it "
                f"was applied. Migrations are immutable after merge. "
                f"Expected checksum: {recorded_checksum[:16]}… "
                f"Actual: {actual_checksum[:16]}… "
                f"Restore the original file or contact the maintainer."
            )

    # --- 5. Identify pending migrations ----------------------------------
    max_applied = max(applied.keys(), default=0)
    pending = [m for m in all_migrations if m.version > max_applied]
    if not pending:
        log.info("No pending migrations.")
        return result

    # --- 6. Take a backup before touching the DB -------------------------
    if backup:
        result.backup_path = _take_backup(db_path)
        _prune_backups(db_path)

    # --- 7. Apply each pending migration ---------------------------------
    for mig in pending:
        _apply_one_migration(mig, db_path, result)

    result.duration_ms = int((time.monotonic() - t_start) * 1000)
    log.info(
        "Applied %d migration(s) in %d ms: %s",
        len(result.applied),
        result.duration_ms,
        result.applied,
    )
    return result
