"""installer/docker.py — Docker presence and version management.

Implements the 4-state detection and consent-aligned install/upgrade logic
from installer/DEPENDENCIES.md §Docker Handling.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable

from installer._run import MissingBinaryError, run_required

_DOCKER_MIN_MAJOR: int = 24


# ── Error classes ─────────────────────────────────────────────────────────────


class DockerError(Exception):
    pass


class DockerMissingError(DockerError):
    pass


class DockerTooOldError(DockerError):
    pass


class DockerDaemonError(DockerError):
    pass


class DockerInstallFailedError(DockerError):
    pass


class DockerVersionUnparseableError(DockerError):
    pass


# ── I/O helpers (replaceable in tests via ensure_docker kwargs) ───────────────


def _has_docker_cmd() -> bool:
    return shutil.which("docker") is not None


def _get_docker_version() -> str | None:
    """Return raw version string from docker daemon, or None if daemon unreachable (D4)."""
    try:
        result = run_required(["docker", "version", "--format", "{{.Server.Version}}"])
    except MissingBinaryError:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _run_docker_install() -> None:
    # F6 fix: guard against curl absence and add pipefail to the bash pipeline
    # so a failing curl propagates exit code rather than silently succeeding.
    if shutil.which("curl") is None:
        raise DockerInstallFailedError(
            "curl is required for get.docker.com but not on PATH. "
            "Install curl first: apt-get install -y curl"
        )
    result = run_required(["bash", "-c", "set -o pipefail; curl -fsSL https://get.docker.com | sh"])
    if result.returncode != 0:
        raise DockerInstallFailedError(
            "Docker installation via get.docker.com failed. "
            "See output above for the failing step. "
            "Manual install: https://docs.docker.com/engine/install/"
        )


def _verify_docker_post_install() -> None:
    """Run post-install checks per DEPENDENCIES.md §Convenience-script invocation."""
    raw = _get_docker_version()
    if raw is None:
        raise DockerInstallFailedError(
            "Post-install check failed: docker daemon not reachable. "
            "Run: journalctl -u docker --no-pager -n 50"
        )
    major, _ = _parse_docker_version(raw)
    if major < _DOCKER_MIN_MAJOR:
        raise DockerInstallFailedError(
            f"Post-install check failed: Docker engine {raw} is below the 24.0 floor. "
            "Run: journalctl -u docker --no-pager -n 50"
        )
    # F3 fix: already present (FileNotFoundError handler); migrate to run_required.
    try:
        compose_result = run_required(["docker", "compose", "version", "--short"])
    except MissingBinaryError as e:
        raise DockerInstallFailedError(
            "Post-install check failed: docker compose plugin not available. "
            "The docker binary was present a moment ago but the compose subcommand "
            "is missing — this typically means docker-compose-plugin was not installed "
            "by the convenience script. Manual fix: apt-get install -y docker-compose-plugin"
        ) from e
    if compose_result.returncode != 0:
        raise DockerInstallFailedError(
            "Post-install check failed: docker compose plugin not found. "
            "Run: journalctl -u docker --no-pager -n 50"
        )


def _prompt_user(question: str) -> bool:
    """Return True if the operator answers y/yes at the interactive prompt."""
    try:
        answer = input(f"{question} [y/N] ").strip().lower()
        return answer in ("y", "yes")
    except EOFError:
        return False


# ── Version parsing ───────────────────────────────────────────────────────────


def _parse_docker_version(raw: str) -> tuple[int, int]:
    """Return (major, minor) from a docker version string like '27.0.3'."""
    # Strip pre-release/build suffixes (e.g. '27.0.3-ce' → '27.0.3')
    clean = raw.split("-")[0]
    parts = clean.split(".")
    try:
        return (int(parts[0]), int(parts[1]))
    except (IndexError, ValueError) as e:
        raise DockerVersionUnparseableError(
            f"Could not parse docker engine version from output: {raw!r}. "
            "This is unusual; please file a bug at "
            "https://github.com/SLOP-Platform/SLOP/issues with the full installer log."
        ) from e


# ── Install/upgrade helpers ───────────────────────────────────────────────────


def _do_install_or_upgrade(
    is_upgrade: bool,
    current_version: str | None,
    consent_mode: str | None,
    run_docker_install: Callable[[], None],
    verify_post_install: Callable[[], None],
    prompt_user: Callable[[str], bool],
) -> None:
    """Resolve consent and run install/upgrade for D1 (absent) or D3 (too old)."""
    if is_upgrade:
        prompt_msg = (
            f"Docker {current_version} is installed but slop requires 24.0+. "
            "Upgrade via get.docker.com?"
        )
        decline_err: DockerError = DockerTooOldError(
            f"Docker {current_version} is installed but slop requires 24.0+. "
            "Run: curl -fsSL https://get.docker.com | sh"
        )
    else:
        prompt_msg = "Docker is not installed. Install via get.docker.com?"
        decline_err = DockerMissingError(
            "Docker is not installed. Install it with: curl -fsSL https://get.docker.com | sh"
        )

    if consent_mode == "yes":
        run_docker_install()
        verify_post_install()
    elif consent_mode == "no":
        raise decline_err
    else:
        # Interactive (consent_mode is None); TTY is guaranteed by install.sh §3
        if prompt_user(prompt_msg):
            run_docker_install()
            verify_post_install()
        else:
            raise decline_err


# ── Public entry point ────────────────────────────────────────────────────────


def ensure_docker(
    consent_mode: str | None = None,
    *,
    has_docker_cmd: Callable[[], bool] = _has_docker_cmd,
    get_docker_version: Callable[[], str | None] = _get_docker_version,
    run_docker_install: Callable[[], None] = _run_docker_install,
    verify_post_install: Callable[[], None] = _verify_docker_post_install,
    prompt_user: Callable[[str], bool] = _prompt_user,
) -> None:
    """Verify or install Docker per DEPENDENCIES.md §Docker Handling.

    consent_mode:
      "yes"  — install/upgrade without prompting
      "no"   — raise if Docker is absent or below the 24.0 floor
      None   — interactive prompt (requires TTY; pipe mode is unreachable here
               per install.sh §3)

    The keyword-only I/O arguments exist solely for unit-test injection;
    production callers pass only consent_mode.
    """
    # D1: Docker absent
    if not has_docker_cmd():
        _do_install_or_upgrade(
            is_upgrade=False,
            current_version=None,
            consent_mode=consent_mode,
            run_docker_install=run_docker_install,
            verify_post_install=verify_post_install,
            prompt_user=prompt_user,
        )
        return

    # Docker binary present: query daemon for version
    raw_version = get_docker_version()

    if raw_version is None:
        # D4: daemon unreachable
        raise DockerDaemonError(
            "Docker is installed but the daemon is not reachable. "
            "Start it with 'systemctl start docker' and re-run the installer."
        )

    major, _ = _parse_docker_version(raw_version)

    if major >= _DOCKER_MIN_MAJOR:
        # D2: present and version >= 24.0 — no-op
        return

    # D3: present but version < 24.0
    _do_install_or_upgrade(
        is_upgrade=True,
        current_version=raw_version,
        consent_mode=consent_mode,
        run_docker_install=run_docker_install,
        verify_post_install=verify_post_install,
        prompt_user=prompt_user,
    )
