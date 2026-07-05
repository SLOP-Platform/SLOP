"""installer/deps_debian.py — apt-managed system dependency installer for Debian/Ubuntu hosts.

Installs curl, netcat-openbsd, and nodejs per installer/DEPENDENCIES.md.
Docker is not handled here; see installer/docker.py::ensure_docker().
"""

from __future__ import annotations

import shutil
from collections.abc import Callable

from installer._run import MissingBinaryError, run_required

# INV-D2 parity target: mirrors installer/DEPENDENCIES.md §Dependency Matrix,
# installer-managed rows.  The docker-engine entry is present for audit parity
# only — installation is delegated to installer/docker.py::ensure_docker().
DEPENDENCIES = [
    {
        "name": "curl",
        "packages": ["curl"],
        "source": "apt-main",
        "min_version": None,
    },
    {
        "name": "netcat-openbsd",
        "packages": ["netcat-openbsd"],
        "source": "apt-main",
        "min_version": None,
    },
    {
        "name": "docker-engine",
        "packages": ["docker-ce", "docker-compose-plugin"],
        "source": "get.docker.com",
        "min_version": (24, 0),
    },
    {
        "name": "nodejs",
        "packages": ["nodejs"],
        "source": "nodesource-22",
        "min_version": (20, 19),
    },
]

_NODE_MIN: tuple[int, int] = (20, 19)


# ── Error classes (§Error Handling) ──────────────────────────────────────────


class DependencyError(Exception):
    pass


class AptUpdateNetworkError(DependencyError):
    pass


class PackageNotFoundError(DependencyError):
    pass


class NodeSourceSetupError(DependencyError):
    pass


class DependencyVersionUnparseableError(DependencyError):
    pass


class AptLockError(DependencyError):
    pass


# ── I/O helpers (replaceable in tests via ensure_dependencies kwargs) ─────────


def _is_pkg_installed(pkg: str) -> bool:
    result = run_required(["dpkg-query", "-W", "-f=${Status}", pkg])
    return result.returncode == 0 and "install ok installed" in result.stdout


def _get_node_version_str() -> str | None:
    try:
        result = run_required(["node", "--version"])
    except MissingBinaryError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _run_apt_update() -> None:
    result = run_required(["apt-get", "update", "-qq"])
    if result.returncode != 0:
        stderr = result.stderr
        if any(
            kw in stderr for kw in ("Failed to fetch", "Could not resolve", "Connection timed out")
        ):
            raise AptUpdateNetworkError(
                "apt-get update failed: network unreachable or apt mirror unavailable. "
                "The installer requires internet access to install packages. "
                "Check connectivity (try: ping deb.debian.org) and re-run."
            )
        raise DependencyError(f"apt-get update failed (exit {result.returncode}): {stderr.strip()}")


def _run_nodesource_setup(distro: str) -> None:
    # F2 fix (defense-in-depth): NodeSource setup requires curl.  Without
    # set -o pipefail, a missing curl silently no-ops and the bash exits 0,
    # leaving nodejs to be installed from distro apt main (Node 18, below floor).
    if shutil.which("curl") is None:
        raise NodeSourceSetupError(
            "NodeSource setup requires curl, but curl is not on PATH. "
            "This indicates an ordering bug in ensure_dependencies — curl should "
            "have been apt-installed before this call. Re-run the installer."
        )
    result = run_required(
        ["bash", "-c", "set -o pipefail; curl -fsSL https://deb.nodesource.com/setup_22.x | bash -"]
    )
    if result.returncode != 0:
        raise NodeSourceSetupError(
            f"NodeSource setup script failed. slop requires Node.js 20.19+ which is not "
            f"available in {distro}'s apt main. Manual install instructions: "
            "https://github.com/nodesource/distributions"
        )


def _run_apt_install(packages: list) -> None:
    result = run_required(["apt-get", "install", "-y", "-qq", "--no-install-recommends", *packages])
    if result.returncode != 0:
        stderr = result.stderr
        if "Could not get lock" in stderr:
            raise AptLockError(
                "apt is currently in use by another process. This is typically "
                "unattended-upgrades running its periodic update on a fresh Ubuntu install. "
                "Wait for it to finish (it usually takes 1-5 minutes) and re-run the installer."
            )
        for pkg in packages:
            if f"Unable to locate package {pkg}" in stderr:
                raise PackageNotFoundError(
                    f"apt cannot find package '{pkg}'. The required apt source may not be "
                    "configured. Re-run the installer with --verbose to see the full apt "
                    "output, or check /etc/apt/sources.list and /etc/apt/sources.list.d/ "
                    "for the expected source."
                )
        raise DependencyError(
            f"apt-get install failed (exit {result.returncode}): {stderr.strip()}"
        )


# ── Version parsing ───────────────────────────────────────────────────────────


def _parse_node_version(raw: str) -> tuple[int, int]:
    """Return (major, minor) from a node version string like 'v20.19.0'."""
    stripped = raw.lstrip("v")
    parts = stripped.split(".")
    try:
        return (int(parts[0]), int(parts[1]))
    except (IndexError, ValueError) as e:
        raise DependencyVersionUnparseableError(
            f"Could not parse nodejs version from output: {raw!r}. "
            "This is unusual; please file a bug at "
            "https://github.com/SLOP-Platform/SLOP/issues with the full installer log."
        ) from e


# ── Public entry point ────────────────────────────────────────────────────────


def ensure_dependencies(
    distro: str = "unknown",
    *,
    is_pkg_installed: Callable[[str], bool] = _is_pkg_installed,
    get_node_version_str: Callable[[], str | None] = _get_node_version_str,
    run_apt_update: Callable[[], None] = _run_apt_update,
    run_nodesource_setup: Callable[[str], None] = _run_nodesource_setup,
    run_apt_install: Callable[[list], None] = _run_apt_install,
) -> list:
    """Install apt-managed deps per DEPENDENCIES.md §Install Ordering.

    Handles curl, netcat-openbsd, and nodejs.  Docker is not handled here.
    Returns the list of package names passed to apt-get install (empty if all
    deps were already present and acceptable).

    The keyword-only I/O arguments exist solely for unit-test injection;
    production callers omit them and get the real system calls.
    """
    to_install: list = []
    needs_nodesource = False

    # curl and netcat-openbsd: no version floor, presence check only
    for pkg in ("curl", "netcat-openbsd"):
        if not is_pkg_installed(pkg):
            to_install.append(pkg)

    # nodejs: check presence and version floor
    node_str = get_node_version_str()
    if node_str is None:
        needs_nodesource = True
        to_install.append("nodejs")
    else:
        node_ver = _parse_node_version(node_str)
        if node_ver < _NODE_MIN:
            # Present but below floor: upgrade via NodeSource (§Error Handling)
            needs_nodesource = True
            to_install.append("nodejs")

    if not to_install:
        return []

    # apt-get update: run if any apt-main package needs installing.
    # NodeSource's setup_22.x runs its own apt-get update internally,
    # so we skip the standalone update when only nodejs is being installed.
    needs_apt_main_update = any(p in to_install for p in ("curl", "netcat-openbsd"))
    if needs_apt_main_update:
        run_apt_update()

    # F2 fix: bootstrap curl BEFORE NodeSource setup when both are needed.
    # NodeSource runs `curl ... | bash -` without pipefail; a missing curl
    # silently succeeds and installs nodejs from distro apt main (Node 18,
    # below the 20.19 floor).  Pre-install curl so _run_nodesource_setup's
    # defense-in-depth check passes and the real curl reaches NodeSource.
    curl_preinstalled = False
    if "curl" in to_install and needs_nodesource:
        run_apt_install(["curl"])
        curl_preinstalled = True
        to_install = [p for p in to_install if p != "curl"]

    # NodeSource setup must precede apt-get install nodejs (§Install Ordering)
    if needs_nodesource:
        run_nodesource_setup(distro)

    if to_install:
        run_apt_install(to_install)

    # Return the full install list including curl even when pre-installed separately.
    if curl_preinstalled:
        return ["curl", *to_install]
    return to_install
