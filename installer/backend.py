"""installer/backend.py — Python venv creation and dependency install step.

setup_backend() creates install_dir/.venv using the host python3, pip-installs
requirements.txt (production deps only), and sets venv ownership to the
slop system user.
"""

from __future__ import annotations

import sys
from pathlib import Path
from collections.abc import Callable

from installer._run import run_required


# ── Error classes ─────────────────────────────────────────────────────────────


class BackendError(Exception):
    pass


class VenvCreationError(BackendError):
    pass


class RequirementsNotFoundError(BackendError):
    pass


class PipInstallError(BackendError):
    pass


# ── I/O helpers (replaceable in tests via setup_backend kwargs) ───────────────


def _venv_exists(venv_dir: Path) -> bool:
    return (venv_dir / "bin" / "python").exists()


def _create_venv(venv_dir: Path) -> None:
    # sys.executable is the Python binary that launched the installer, which
    # install.sh selects to be python3.11 on Ubuntu 22.04 (via _MS_PYTHON3).
    # Using "python3" here would resolve to whatever /usr/bin/python3 points
    # to, which on Ubuntu 22.04 is python3.10 (no apt_pkg breakage desired).
    result = run_required([sys.executable, "-m", "venv", str(venv_dir)])
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if "ensurepip" in stderr:
            raise VenvCreationError(
                f"python3 -m venv failed at {venv_dir} — ensurepip is not available. "
                "Install the venv package first: apt-get install -y python3-venv "
                f"(original error: {stderr})"
            )
        raise VenvCreationError(f"python3 -m venv failed at {venv_dir}: {stderr}")


def _requirements_exists(req_path: Path) -> bool:
    return req_path.is_file()


def _run_pip_install(pip_path: Path, req_path: Path) -> None:
    result = run_required([str(pip_path), "install", "-r", str(req_path)])
    if result.returncode != 0:
        raise PipInstallError(
            f"pip install failed (exit {result.returncode}): {result.stderr.strip()}"
        )


def _run_chown(user: str, venv_dir: Path) -> None:
    spec = f"{user}:{user}"
    result = run_required(["chown", "-R", spec, str(venv_dir)])
    if result.returncode != 0:
        raise BackendError(f"chown -R {spec} {venv_dir} failed: {result.stderr.strip()}")


# ── Public entry point ────────────────────────────────────────────────────────


def setup_backend(
    install_dir,
    *,
    user: str = "slop",
    venv_exists: Callable[[Path], bool] = _venv_exists,
    create_venv: Callable[[Path], None] = _create_venv,
    requirements_exists: Callable[[Path], bool] = _requirements_exists,
    run_pip_install: Callable[[Path, Path], None] = _run_pip_install,
    run_chown: Callable[[str, Path], None] = _run_chown,
) -> None:
    """Create install_dir/.venv, install requirements.txt, and chown to user.

    Idempotency: if .venv/bin/python already exists, venv creation is skipped.
    pip install and chown always run — pip is idempotent for satisfied
    requirements, and chown is idempotent by nature.

    requirements.txt must exist at install_dir/requirements.txt; this is
    guaranteed if fetch_repo() succeeded immediately before this call.

    The keyword-only I/O arguments exist solely for unit-test injection;
    production callers pass only install_dir and optionally user.
    """
    dest = Path(install_dir)
    venv_dir = dest / ".venv"
    req_path = dest / "requirements.txt"
    pip_path = venv_dir / "bin" / "pip"

    if not venv_exists(venv_dir):
        create_venv(venv_dir)

    if not requirements_exists(req_path):
        raise RequirementsNotFoundError(
            f"requirements.txt not found at {req_path}. "
            "Ensure fetch_repo() succeeded before calling setup_backend()."
        )

    run_pip_install(pip_path, req_path)
    run_chown(user, venv_dir)
