"""installer/frontend.py — npm install and build step for the v5 installer.

build_frontend() runs `npm ci` then `npm run build` inside install_dir/frontend/.
The vite config emits build artifacts to install_dir/backend/static/.
"""

from __future__ import annotations

from pathlib import Path
from collections.abc import Callable

from installer._run import MissingBinaryError, run_required


# ── Error classes ─────────────────────────────────────────────────────────────


class FrontendError(Exception):
    pass


class PackageJsonNotFoundError(FrontendError):
    pass


class NpmCiError(FrontendError):
    pass


class NpmBuildError(FrontendError):
    pass


# ── I/O helpers (replaceable in tests via build_frontend kwargs) ──────────────


def _package_json_exists(frontend_dir: Path) -> bool:
    return (frontend_dir / "package.json").is_file()


def _build_exists(static_dir: Path) -> bool:
    index = static_dir / "index.html"
    if not index.exists():
        return False
    # Verify the JS bundle referenced in index.html actually exists.
    # A committed-but-stale index.html referencing a different build's hashed
    # asset would otherwise fool this check and produce a blank page.
    import re as _re

    html = index.read_text(errors="replace")
    m = _re.search(r'src="/assets/(index-[^"]+\.js)"', html)
    if not m:
        return False
    return (static_dir / "assets" / m.group(1)).exists()


def _run_npm_ci(frontend_dir: Path) -> None:
    # F13: pre-check dir existence before cwd= subprocess — both raise
    # FileNotFoundError but for different reasons; pre-check gives a clear message.
    if not frontend_dir.is_dir():
        raise NpmCiError(
            f"npm ci: frontend directory does not exist: {frontend_dir}. "
            "Ensure fetch_repo() completed before calling build_frontend()."
        )
    try:
        result = run_required(["npm", "ci"], cwd=str(frontend_dir))
    except MissingBinaryError as e:
        # F4: npm not on PATH — means deps_debian.ensure_dependencies() did not
        # install nodejs (or NodeSource setup silently no-op'd per F2).
        raise NpmCiError(
            "npm is not on PATH. ensure_dependencies() should have installed nodejs "
            "with npm bundled — this indicates a NodeSource setup failure (F2) or a "
            "corrupt nodejs install. Diagnose: which node && node --version && which npm"
        ) from e
    if result.returncode != 0:
        raise NpmCiError(f"npm ci failed (exit {result.returncode}): {result.stderr.strip()}")


def _run_npm_build(frontend_dir: Path) -> None:
    # F13: same pre-check as _run_npm_ci (same cwd= subprocess failure mode).
    if not frontend_dir.is_dir():
        raise NpmBuildError(
            f"npm run build: frontend directory does not exist: {frontend_dir}. "
            "Ensure fetch_repo() completed before calling build_frontend()."
        )
    try:
        result = run_required(["npm", "run", "build"], cwd=str(frontend_dir))
    except MissingBinaryError as e:
        # F4: same npm-absent failure mode as _run_npm_ci.
        raise NpmBuildError(
            "npm is not on PATH. ensure_dependencies() should have installed nodejs "
            "with npm bundled — this indicates a NodeSource setup failure (F2) or a "
            "corrupt nodejs install. Diagnose: which node && node --version && which npm"
        ) from e
    if result.returncode != 0:
        raise NpmBuildError(
            f"npm run build failed (exit {result.returncode}): {result.stderr.strip()}"
        )


# ── Public entry point ────────────────────────────────────────────────────────


def build_frontend(
    install_dir,
    *,
    package_json_exists: Callable[[Path], bool] = _package_json_exists,
    build_exists: Callable[[Path], bool] = _build_exists,
    run_npm_ci: Callable[[Path], None] = _run_npm_ci,
    run_npm_build: Callable[[Path], None] = _run_npm_build,
) -> None:
    """Run npm ci + npm run build inside install_dir/frontend/.

    Build artifacts are emitted to install_dir/backend/static/ (vite.config.ts
    outDir: '../backend/static').

    Prerequisites: package.json must exist at install_dir/frontend/package.json.
    Raises PackageJsonNotFoundError immediately if absent.

    Idempotency: if install_dir/backend/static/index.html already exists, both
    npm ci and npm run build are skipped — the build output is already in place.

    The keyword-only I/O arguments exist solely for unit-test injection;
    production callers pass only install_dir.
    """
    dest = Path(install_dir)
    frontend_dir = dest / "frontend"
    static_dir = dest / "backend" / "static"

    if not package_json_exists(frontend_dir):
        raise PackageJsonNotFoundError(
            f"package.json not found at {frontend_dir / 'package.json'}. "
            "Ensure fetch_repo() succeeded before calling build_frontend()."
        )

    if build_exists(static_dir):
        return

    run_npm_ci(frontend_dir)
    run_npm_build(frontend_dir)
