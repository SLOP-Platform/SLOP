"""installer/prereq.py — prerequisite checks for the v5 installer.

check_prereqs() runs all checks and returns a list of PrereqFinding.
No check short-circuits on failure; all run to completion so the operator
gets a full picture before re-running.

Checks (V5_INSTALLER_PLAN.md Step 1.4.a):
  kernel     — kernel release ≥ 5.10
  disk       — ≥ 10 GiB free at the install path (or nearest existing ancestor)
  port N     — backend port (default 8080) is not in use
  port 80    — HTTP reverse-proxy port is not in use
  port 443   — HTTPS reverse-proxy port is not in use
  root       — installer is running as root (euid == 0)
  systemd    — PID 1 is systemd
  docker     — Docker Engine ≥ 24.0 is present and daemon is reachable
  compose    — Docker Compose v2 plugin (docker compose) is available
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from installer._run import run_required

_MIN_KERNEL: tuple[int, int] = (5, 10)
_MIN_DISK_BYTES: int = 10 * 1024 * 1024 * 1024  # 10 GiB
_PROXY_PORTS: tuple[tuple[int, str], ...] = ((80, "http"), (443, "https"))


@dataclass
class PrereqFinding:
    """Result of a single prerequisite check."""

    name: str
    ok: bool
    remediation: str  # empty string when ok is True


# ── Low-level I/O helpers (replaceable in tests via check_prereqs kwargs) ─────


def _read_kernel_release() -> str:
    return platform.release()


def _get_disk_free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def _is_port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind(("", port))
            return True
        except OSError:
            return False


def _get_effective_uid() -> int:
    return os.geteuid()


def _read_init_comm() -> str:
    return Path("/proc/1/comm").read_text(encoding="utf-8").strip()


def _get_docker_version() -> str | None:
    """Return Docker Server version string, or None when absent/unreachable."""
    if shutil.which("docker") is None:
        return None
    try:
        r = run_required(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            timeout=10,
        )
        if r.returncode != 0:
            return None
        return r.stdout.strip() or None
    except Exception:
        return None


def _has_compose_plugin() -> bool:
    """Return True when `docker compose version` exits 0."""
    if shutil.which("docker") is None:
        return False
    try:
        r = run_required(
            ["docker", "compose", "version"],
            timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


# ── Kernel version parsing ────────────────────────────────────────────────────


def _parse_kernel_version(release: str) -> tuple[int, int]:
    """Return (major, minor) from a kernel release string.

    Handles strings like '5.15.0-100-generic' and
    '6.6.87.2-microsoft-standard-WSL2'. Returns (0, 0) on parse failure.
    """
    numeric_part = release.split("-")[0]
    parts = numeric_part.split(".")
    try:
        major = int(parts[0]) if len(parts) > 0 else 0
        minor = int(parts[1]) if len(parts) > 1 else 0
    except ValueError:
        return (0, 0)
    return (major, minor)


# ── Individual check implementations ─────────────────────────────────────────


def _check_kernel(
    read_kernel_release: Callable[[], str],
) -> PrereqFinding:
    release = read_kernel_release()
    version = _parse_kernel_version(release)
    ok = version >= _MIN_KERNEL
    return PrereqFinding(
        name="kernel version",
        ok=ok,
        remediation=(
            ""
            if ok
            else (
                f"Kernel {release!r} is below the required minimum "
                f"{_MIN_KERNEL[0]}.{_MIN_KERNEL[1]}. "
                "Upgrade the host kernel to 5.10 or later before running "
                "the installer."
            )
        ),
    )


def _check_disk(
    install_path: Path,
    get_disk_free_bytes: Callable[[Path], int],
) -> PrereqFinding:
    # shutil.disk_usage requires an existing path; walk up to the nearest
    # ancestor that exists so the check works before install_path is created.
    check_path = install_path
    while not check_path.exists() and check_path != check_path.parent:
        check_path = check_path.parent

    free = get_disk_free_bytes(check_path)
    ok = free >= _MIN_DISK_BYTES
    free_gib = free / (1024**3)
    return PrereqFinding(
        name="disk space",
        ok=ok,
        remediation=(
            ""
            if ok
            else (
                f"Only {free_gib:.1f} GiB free on the filesystem containing "
                f"{install_path}; the installer requires at least 10 GiB. "
                "Free disk space or choose a different --install-dir."
            )
        ),
    )


def _check_port(
    port: int,
    label: str,
    is_port_free: Callable[[int], bool],
) -> PrereqFinding:
    ok = is_port_free(port)
    return PrereqFinding(
        name=f"port {port} ({label})",
        ok=ok,
        remediation=(
            ""
            if ok
            else (
                f"Port {port} ({label}) is already in use. "
                f"Stop whatever is listening on port {port} before running "
                "the installer."
            )
        ),
    )


def _check_root(
    get_effective_uid: Callable[[], int],
) -> PrereqFinding:
    ok = get_effective_uid() == 0
    return PrereqFinding(
        name="root access",
        ok=ok,
        remediation=(
            "" if ok else "The installer must run as root. Re-run with: sudo ./install.sh"
        ),
    )


def _check_systemd(
    read_init_comm: Callable[[], str],
) -> PrereqFinding:
    try:
        comm = read_init_comm()
        ok = comm == "systemd"
    except OSError:
        comm = "<unreadable>"
        ok = False
    return PrereqFinding(
        name="systemd init",
        ok=ok,
        remediation=(
            ""
            if ok
            else (
                f"PID 1 appears to be {comm!r}, not 'systemd'. "
                "slop v5 requires systemd as the init system. "
                "sysvinit, openrc, runit, and s6 are not supported in v5.0."
            )
        ),
    )


_DOCKER_MIN_MAJOR: int = 24


def _check_docker(
    get_docker_version: Callable[[], str | None],
) -> PrereqFinding:
    """Check Docker Engine is present, daemon is reachable, and version ≥ 24.0."""
    raw = get_docker_version()
    if raw is None:
        return PrereqFinding(
            name="docker engine",
            ok=False,
            remediation=(
                "Docker Engine is not installed or the daemon is not reachable. "
                "Install Docker with: curl -fsSL https://get.docker.com | sh  "
                "then start the daemon: systemctl start docker"
            ),
        )
    # Parse major version
    try:
        major = int(raw.split(".")[0])
    except (ValueError, IndexError):
        major = 0
    ok = major >= _DOCKER_MIN_MAJOR
    return PrereqFinding(
        name="docker engine",
        ok=ok,
        remediation=(
            ""
            if ok
            else (
                f"Docker Engine {raw!r} is below the required minimum 24.0. "
                "Upgrade with: curl -fsSL https://get.docker.com | sh"
            )
        ),
    )


def _check_compose(
    has_compose_plugin: Callable[[], bool],
) -> PrereqFinding:
    """Check the Docker Compose v2 plugin (`docker compose`) is available."""
    ok = has_compose_plugin()
    return PrereqFinding(
        name="docker compose plugin",
        ok=ok,
        remediation=(
            ""
            if ok
            else (
                "The Docker Compose v2 plugin is not available. "
                "Install it with: apt-get install -y docker-compose-plugin  "
                "or ensure Docker Desktop includes Compose (Windows/macOS)."
            )
        ),
    )


# ── Public entry point ────────────────────────────────────────────────────────


def check_prereqs(
    install_path: str | Path,
    port: int = 8080,
    *,
    read_kernel_release: Callable[[], str] = _read_kernel_release,
    get_disk_free_bytes: Callable[[Path], int] = _get_disk_free_bytes,
    is_port_free: Callable[[int], bool] = _is_port_free,
    get_effective_uid: Callable[[], int] = _get_effective_uid,
    read_init_comm: Callable[[], str] = _read_init_comm,
    get_docker_version: Callable[[], str | None] = _get_docker_version,
    has_compose_plugin: Callable[[], bool] = _has_compose_plugin,
) -> list[PrereqFinding]:
    """Run all prerequisite checks and return the full list of findings.

    All checks run regardless of whether earlier checks fail. The caller
    decides how to handle failing findings; this function only gathers them.

    The keyword-only I/O arguments exist solely for unit-test injection;
    production callers omit them and get the real system calls.
    """
    p = Path(install_path)
    findings: list[PrereqFinding] = []

    findings.append(_check_kernel(read_kernel_release))
    findings.append(_check_disk(p, get_disk_free_bytes))
    findings.append(_check_port(port, "backend", is_port_free))
    for proxy_port, label in _PROXY_PORTS:
        findings.append(_check_port(proxy_port, label, is_port_free))
    findings.append(_check_root(get_effective_uid))
    findings.append(_check_systemd(read_init_comm))
    findings.append(_check_docker(get_docker_version))
    findings.append(_check_compose(has_compose_plugin))

    return findings
