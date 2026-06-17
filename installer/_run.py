"""installer/_run.py — shared subprocess wrapper for consistent error mapping.

All installer modules MUST use run_required() for subprocess invocations.
Bare subprocess.run in installer/*.py (except this file) is a Class-A audit
failure detected by tools/check_structural_antipatterns.py rule-006.
"""

from __future__ import annotations

import subprocess


# ── Exception classes ─────────────────────────────────────────────────────────


class MissingBinaryError(Exception):
    """Required external binary is not on PATH.

    Raised by run_required() when subprocess.run raises FileNotFoundError.
    Distinct from RunFailedError so callers can catch the structural failure
    mode (binary absent) separately from the binary-ran-but-failed mode.
    """


class RunFailedError(Exception):
    """External command exited with a non-zero return code.

    Defined here for modules that want a generic 'command failed' error.
    Modules with stderr-pattern-based classification (AptLockError,
    PackageNotFoundError, etc.) raise their domain-specific subclasses instead.
    """

    def __init__(self, cmd: list, returncode: int, stderr: str) -> None:
        binary = cmd[0] if cmd else "<unknown>"
        super().__init__(f"{binary!r} failed (exit {returncode}): {stderr.strip()}")
        self.cmd = cmd
        self.returncode = returncode
        self.stderr = stderr


class RunTimeoutError(Exception):
    """External command exceeded its configured timeout.

    Raised by run_required() when subprocess raises subprocess.TimeoutExpired.
    """

    def __init__(self, cmd: list, timeout: float | None) -> None:
        binary = cmd[0] if cmd else "<unknown>"
        super().__init__(f"{binary!r} timed out after {timeout}s")
        self.cmd = cmd
        self.timeout = timeout


# ── run_required ──────────────────────────────────────────────────────────────


def run_required(
    cmd: list,
    *,
    cwd: str | None = None,
    env=None,
    check_output: bool = False,
    timeout: float | None = None,
) -> subprocess.CompletedProcess | str:
    """Run an external command, mapping OS-level errors to installer exceptions.

    Returns CompletedProcess when check_output=False (caller checks returncode).
    Returns stdout str when check_output=True.

    Does NOT raise on non-zero returncode by default — callers inspect
    result.returncode and map to their domain-specific exceptions.

    Raises:
        MissingBinaryError — cmd[0] not on PATH (FileNotFoundError from OS)
        RunTimeoutError    — command exceeded timeout
    """
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except FileNotFoundError as e:
        raise MissingBinaryError(
            f"Required binary not on PATH: {cmd[0]!r}. "
            f"Full command: {' '.join(str(a) for a in cmd)}"
        ) from e
    except subprocess.TimeoutExpired as e:
        raise RunTimeoutError(cmd, timeout) from e
    if check_output:
        return result.stdout
    return result
