"""installer/data_dir.py — runtime data directory creation step for the v5 installer.

ensure_data_dir() creates the configured data_dir, sets ownership to
slop:slop, and sets mode 0750 per ADR 0013 §1.  Idempotent:
mkdir uses exist_ok=True (no error if the directory already exists); chown
and chmod always run — both are inherently idempotent.
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable

from installer._run import run_required


# ── Error classes ─────────────────────────────────────────────────────────────


class DataDirError(Exception):
    pass


class DataDirCreationError(DataDirError):
    pass


class DataDirChownError(DataDirError):
    pass


class DataDirChmodError(DataDirError):
    pass


# ── I/O helpers (replaceable in tests via ensure_data_dir kwargs) ─────────────


def _make_dir(data_dir: Path) -> None:
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DataDirCreationError(f"Failed to create data directory {data_dir}: {exc}") from exc


def _run_chown(user: str, group: str, data_dir: Path) -> None:
    spec = f"{user}:{group}"
    result = run_required(["chown", spec, str(data_dir)])
    if result.returncode != 0:
        raise DataDirChownError(f"chown {spec} {data_dir} failed: {result.stderr.strip()}")


def _run_chmod(mode: int, data_dir: Path) -> None:
    mode_str = oct(mode)[2:]
    result = run_required(["chmod", mode_str, str(data_dir)])
    if result.returncode != 0:
        raise DataDirChmodError(f"chmod {mode_str} {data_dir} failed: {result.stderr.strip()}")


# ── Public entry point ────────────────────────────────────────────────────────


def ensure_data_dir(
    data_dir,
    *,
    user: str = "slop",
    group: str = "slop",
    mode: int = 0o750,
    make_dir: Callable[[Path], None] = _make_dir,
    run_chown: Callable[[str, str, Path], None] = _run_chown,
    run_chmod: Callable[[int, Path], None] = _run_chmod,
) -> None:
    """Create data_dir with owner slop:slop and mode 0750.

    Per ADR 0013 §1 the installer creates only the top-level directory;
    the backend writes inside it.  On upgrade with --force the data dir is
    preserved unchanged (data-preservation contract, V5_INSTALLER_PLAN.md §4.2).

    The keyword-only I/O arguments exist solely for unit-test injection;
    production callers pass only data_dir and optionally user/group/mode.
    """
    p = Path(data_dir)
    make_dir(p)
    run_chown(user, group, p)
    run_chmod(mode, p)
