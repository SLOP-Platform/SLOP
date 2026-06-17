"""installer/tests/test_install_sh.py — shell-level tests for install.sh.

Tests the pipe-mode fail-fast introduced in Step 1.3.a and specified in
Step 4.3.c. Uses subprocess.run with stdin=DEVNULL to simulate pipe mode
(no TTY). The TTY interactive path is verified on real VMs (Step 4.5).

All tests run bash install.sh directly; no root required because pipe-mode
validation fires before the root check (ADR 0013 §3 ordering guarantee).
"""

import subprocess
from pathlib import Path


_REPO_ROOT = Path(__file__).parent.parent.parent
_INSTALL_SH = _REPO_ROOT / "install.sh"

# Phrase from the pipe-mode remediation message (install.sh lines 60-61).
_REMEDIATION_PHRASE = "Pipe mode requires an explicit Docker decision"


def _run_pipe(*extra_args: str) -> subprocess.CompletedProcess:
    """Run install.sh with stdin=DEVNULL (simulates pipe / no-TTY) and capture output."""
    return subprocess.run(
        ["bash", str(_INSTALL_SH), *extra_args],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )


class TestPipeModeFastFail:
    """Step 4.3.c — pipe-stdin validation in install.sh.

    ADR 0013 §3: pipe-mode check fires before any filesystem write, root
    check, apt call, or distro detection. Tests exploit this ordering —
    they do not need root and do not modify the system.
    """

    def test_pipe_without_flag_exits_nonzero(self):
        result = _run_pipe()
        assert result.returncode != 0

    def test_pipe_without_flag_prints_remediation_to_stderr(self):
        result = _run_pipe()
        assert _REMEDIATION_PHRASE in result.stderr

    def test_pipe_without_flag_names_both_flag_values(self):
        result = _run_pipe()
        assert "--install-docker=yes" in result.stderr
        assert "--install-docker=no" in result.stderr

    def test_pipe_without_flag_states_no_files_written(self):
        result = _run_pipe()
        assert "No files have been written" in result.stderr

    def test_pipe_with_docker_yes_passes_pipe_check(self):
        # Fails at root check (exit nonzero) but NOT with the remediation message.
        result = _run_pipe("--install-docker=yes")
        assert _REMEDIATION_PHRASE not in result.stderr

    def test_pipe_with_docker_no_passes_pipe_check(self):
        # Interpretation A: --install-docker=no is a valid pipe-mode invocation.
        result = _run_pipe("--install-docker=no")
        assert _REMEDIATION_PHRASE not in result.stderr

    def test_pipe_with_docker_yes_fails_at_root_check(self):
        # Verifies ordering: pipe-mode check passes, execution reaches the root
        # check and fails there (not at the pipe-mode guard).
        result = _run_pipe("--install-docker=yes")
        assert result.returncode != 0
        assert "root" in result.stderr.lower()
