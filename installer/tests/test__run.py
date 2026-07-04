"""installer/tests/test__run.py — unit tests for installer/_run.py.

All subprocess I/O is mocked at the subprocess.run boundary.

Coverage:
  TestMissingBinaryError — FileNotFoundError → MissingBinaryError
  TestRunTimeoutError    — TimeoutExpired → RunTimeoutError
  TestRunFailedError     — exception class construction
  TestRunRequired        — success paths (CompletedProcess + check_output)
  TestRunRequiredPassthrough — cwd, env, timeout forwarded to subprocess.run
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from installer._run import (
    MissingBinaryError,
    RunFailedError,
    RunTimeoutError,
    run_required,
)


# ── TestMissingBinaryError ────────────────────────────────────────────────────


class TestMissingBinaryError:
    def test_filenotfound_raises_missing_binary_error(self):
        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("git")):
            with pytest.raises(MissingBinaryError):
                run_required(["git", "--version"])

    def test_missing_binary_error_names_binary(self):
        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("apt-get")):
            with pytest.raises(MissingBinaryError, match="apt-get"):
                run_required(["apt-get", "update"])

    def test_missing_binary_error_message_contains_full_command(self):
        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("docker")):
            with pytest.raises(MissingBinaryError, match="compose"):
                run_required(["docker", "compose", "version"])


# ── TestRunTimeoutError ───────────────────────────────────────────────────────


class TestRunTimeoutError:
    def test_timeout_expired_raises_run_timeout_error(self):
        with patch(
            "installer._run.subprocess.run", side_effect=subprocess.TimeoutExpired(["git"], 5.0)
        ):
            with pytest.raises(RunTimeoutError):
                run_required(["git", "ls-remote"], timeout=5.0)

    def test_run_timeout_error_message_names_binary(self):
        with patch(
            "installer._run.subprocess.run", side_effect=subprocess.TimeoutExpired(["curl"], 10.0)
        ):
            with pytest.raises(RunTimeoutError, match="curl"):
                run_required(["curl", "https://example.com"], timeout=10.0)

    def test_run_timeout_error_stores_timeout(self):
        with patch(
            "installer._run.subprocess.run", side_effect=subprocess.TimeoutExpired(["git"], 30.0)
        ):
            try:
                run_required(["git", "clone"], timeout=30.0)
            except RunTimeoutError as exc:
                assert exc.timeout == 30.0


# ── TestRunFailedError ────────────────────────────────────────────────────────


class TestRunFailedError:
    def test_stores_returncode(self):
        exc = RunFailedError(["apt-get", "install"], 1, "E: Couldn't find package foo")
        assert exc.returncode == 1

    def test_stores_stderr(self):
        exc = RunFailedError(["npm", "ci"], 1, "ERESOLVE")
        assert exc.stderr == "ERESOLVE"

    def test_message_names_binary_and_returncode(self):
        exc = RunFailedError(["apt-get", "update"], 100, "network error")
        assert "apt-get" in str(exc)
        assert "100" in str(exc)


# ── TestRunRequired ───────────────────────────────────────────────────────────


class TestRunRequired:
    def test_success_returns_completed_process(self):
        fake = MagicMock(returncode=0, stdout="v1.0\n", stderr="")
        with patch("installer._run.subprocess.run", return_value=fake):
            result = run_required(["git", "--version"])
        assert result.returncode == 0

    def test_check_output_true_returns_stdout_string(self):
        fake = MagicMock(returncode=0, stdout="v22.0.0\n", stderr="")
        with patch("installer._run.subprocess.run", return_value=fake):
            result = run_required(["node", "--version"], check_output=True)
        assert result == "v22.0.0\n"

    def test_check_output_false_returns_completed_process(self):
        fake = MagicMock(returncode=0, stdout="ok", stderr="")
        with patch("installer._run.subprocess.run", return_value=fake):
            result = run_required(["systemctl", "status"], check_output=False)
        assert hasattr(result, "returncode")

    def test_nonzero_returncode_returned_not_raised_by_default(self):
        fake = MagicMock(returncode=1, stdout="", stderr="error")
        with patch("installer._run.subprocess.run", return_value=fake):
            result = run_required(["bad-command"])
        assert result.returncode == 1  # caller must check; no automatic raise


# ── TestRunRequiredPassthrough ────────────────────────────────────────────────


class TestRunRequiredPassthrough:
    def test_cwd_forwarded_to_subprocess(self):
        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch("installer._run.subprocess.run", return_value=fake) as mock_run:
            run_required(["npm", "ci"], cwd="/some/dir")
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["cwd"] == "/some/dir"

    def test_timeout_forwarded_to_subprocess(self):
        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch("installer._run.subprocess.run", return_value=fake) as mock_run:
            run_required(["curl", "https://example.com"], timeout=30.0)
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs["timeout"] == 30.0

    def test_capture_output_always_true(self):
        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch("installer._run.subprocess.run", return_value=fake) as mock_run:
            run_required(["ls"])
        mock_run.assert_called_once()
        assert mock_run.call_args.kwargs.get("capture_output") is True
