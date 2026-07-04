"""installer/tests/test_backend.py — unit tests for installer/backend.py.

All filesystem and subprocess I/O is mocked via setup_backend keyword-only
injection.  No real venv creation, no real pip invocations, no real chown.

Coverage:
  TestSetupBackendVenvCreation — venv absent vs present (idempotency); error propagation
  TestSetupBackendRequirements — missing requirements.txt raises with clear message
  TestSetupBackendPipInstall   — pip called with correct args; failure propagation
  TestSetupBackendChown        — chown ordering, user, venv_dir target
  TestSetupBackendOrdering     — full call sequence: create_venv → pip → chown
"""

from __future__ import annotations

from pathlib import Path

import pytest

from installer._run import MissingBinaryError
from installer.backend import (
    PipInstallError,
    RequirementsNotFoundError,
    VenvCreationError,
    _create_venv,
    _run_chown,
    _run_pip_install,
    setup_backend,
)


# ── Shared helpers ────────────────────────────────────────────────────────────


def _noop(*_args, **_kwargs):
    pass


def _passing_kwargs(
    venv_present: bool = False,
    req_present: bool = True,
) -> dict:
    """Return setup_backend injectable kwargs for a default-success scenario."""
    return {
        "venv_exists": lambda venv_dir: venv_present,
        "create_venv": _noop,
        "requirements_exists": lambda req_path: req_present,
        "run_pip_install": _noop,
        "run_chown": _noop,
    }


# ── TestSetupBackendVenvCreation ──────────────────────────────────────────────


class TestSetupBackendVenvCreation:
    def test_create_venv_called_when_absent(self):
        calls = []
        kwargs = _passing_kwargs(venv_present=False)
        kwargs["create_venv"] = lambda venv_dir: calls.append(venv_dir)
        setup_backend("/some/dir", **kwargs)
        assert len(calls) == 1

    def test_create_venv_receives_correct_path(self):
        paths = []
        kwargs = _passing_kwargs(venv_present=False)
        kwargs["create_venv"] = lambda venv_dir: paths.append(venv_dir)
        setup_backend("/some/dir", **kwargs)
        assert paths[0] == Path("/some/dir/.venv")

    def test_create_venv_skipped_when_present(self):
        calls = []
        kwargs = _passing_kwargs(venv_present=True)
        kwargs["create_venv"] = lambda venv_dir: calls.append(venv_dir)
        setup_backend("/some/dir", **kwargs)
        assert calls == []

    def test_venv_creation_error_propagates(self):
        def fail_create(venv_dir):
            raise VenvCreationError("python3 -m venv failed")

        kwargs = _passing_kwargs(venv_present=False)
        kwargs["create_venv"] = fail_create
        with pytest.raises(VenvCreationError):
            setup_backend("/some/dir", **kwargs)

    def test_venv_creation_error_skips_pip(self):
        pip_calls = []

        def fail_create(venv_dir):
            raise VenvCreationError("failed")

        kwargs = _passing_kwargs(venv_present=False)
        kwargs["create_venv"] = fail_create
        kwargs["run_pip_install"] = lambda pip, req: pip_calls.append(1)
        with pytest.raises(VenvCreationError):
            setup_backend("/some/dir", **kwargs)
        assert pip_calls == []


# ── TestSetupBackendRequirements ──────────────────────────────────────────────


class TestSetupBackendRequirements:
    def test_missing_requirements_raises(self):
        kwargs = _passing_kwargs(req_present=False)
        with pytest.raises(RequirementsNotFoundError):
            setup_backend("/some/dir", **kwargs)

    def test_missing_requirements_message_names_path(self):
        kwargs = _passing_kwargs(req_present=False)
        with pytest.raises(RequirementsNotFoundError, match=r"requirements.txt"):
            setup_backend("/some/dir", **kwargs)

    def test_missing_requirements_skips_pip(self):
        pip_calls = []
        kwargs = _passing_kwargs(req_present=False)
        kwargs["run_pip_install"] = lambda pip, req: pip_calls.append(1)
        with pytest.raises(RequirementsNotFoundError):
            setup_backend("/some/dir", **kwargs)
        assert pip_calls == []

    def test_requirements_exists_check_uses_correct_path(self):
        paths_checked = []
        kwargs = _passing_kwargs()
        kwargs["requirements_exists"] = lambda p: paths_checked.append(p) or True
        setup_backend("/some/dir", **kwargs)
        assert paths_checked == [Path("/some/dir/requirements.txt")]


# ── TestSetupBackendPipInstall ────────────────────────────────────────────────


class TestSetupBackendPipInstall:
    def test_pip_install_called(self):
        calls = []
        kwargs = _passing_kwargs()
        kwargs["run_pip_install"] = lambda pip, req: calls.append((pip, req))
        setup_backend("/some/dir", **kwargs)
        assert len(calls) == 1

    def test_pip_receives_correct_pip_path(self):
        calls = []
        kwargs = _passing_kwargs()
        kwargs["run_pip_install"] = lambda pip, req: calls.append((pip, req))
        setup_backend("/some/dir", **kwargs)
        pip_path, _ = calls[0]
        assert pip_path == Path("/some/dir/.venv/bin/pip")

    def test_pip_receives_correct_req_path(self):
        calls = []
        kwargs = _passing_kwargs()
        kwargs["run_pip_install"] = lambda pip, req: calls.append((pip, req))
        setup_backend("/some/dir", **kwargs)
        _, req_path = calls[0]
        assert req_path == Path("/some/dir/requirements.txt")

    def test_pip_failure_propagates(self):
        def fail_pip(pip, req):
            raise PipInstallError("pip exit 1")

        kwargs = _passing_kwargs()
        kwargs["run_pip_install"] = fail_pip
        with pytest.raises(PipInstallError):
            setup_backend("/some/dir", **kwargs)

    def test_pip_failure_skips_chown(self):
        chown_calls = []

        def fail_pip(pip, req):
            raise PipInstallError("failed")

        kwargs = _passing_kwargs()
        kwargs["run_pip_install"] = fail_pip
        kwargs["run_chown"] = lambda user, venv_dir: chown_calls.append(1)
        with pytest.raises(PipInstallError):
            setup_backend("/some/dir", **kwargs)
        assert chown_calls == []

    def test_pip_called_when_venv_already_exists(self):
        pip_calls = []
        kwargs = _passing_kwargs(venv_present=True)
        kwargs["run_pip_install"] = lambda pip, req: pip_calls.append(1)
        setup_backend("/some/dir", **kwargs)
        assert len(pip_calls) == 1


# ── TestSetupBackendChown ─────────────────────────────────────────────────────


class TestSetupBackendChown:
    def test_chown_called(self):
        calls = []
        kwargs = _passing_kwargs()
        kwargs["run_chown"] = lambda user, venv_dir: calls.append((user, venv_dir))
        setup_backend("/some/dir", **kwargs)
        assert len(calls) == 1

    def test_chown_default_user_is_slop(self):
        calls = []
        kwargs = _passing_kwargs()
        kwargs["run_chown"] = lambda user, venv_dir: calls.append((user, venv_dir))
        setup_backend("/some/dir", **kwargs)
        user, _ = calls[0]
        assert user == "slop"

    def test_chown_custom_user_passed_through(self):
        calls = []
        kwargs = _passing_kwargs()
        kwargs["run_chown"] = lambda user, venv_dir: calls.append((user, venv_dir))
        setup_backend("/some/dir", user="testuser", **kwargs)
        user, _ = calls[0]
        assert user == "testuser"

    def test_chown_receives_correct_venv_dir(self):
        calls = []
        kwargs = _passing_kwargs()
        kwargs["run_chown"] = lambda user, venv_dir: calls.append((user, venv_dir))
        setup_backend("/some/dir", **kwargs)
        _, venv_dir = calls[0]
        assert venv_dir == Path("/some/dir/.venv")

    def test_chown_called_when_venv_already_exists(self):
        calls = []
        kwargs = _passing_kwargs(venv_present=True)
        kwargs["run_chown"] = lambda user, venv_dir: calls.append(1)
        setup_backend("/some/dir", **kwargs)
        assert len(calls) == 1


# ── TestSetupBackendOrdering ──────────────────────────────────────────────────


class TestSetupBackendOrdering:
    def test_full_call_order_create_pip_chown(self):
        call_order = []
        kwargs = _passing_kwargs(venv_present=False)
        kwargs["create_venv"] = lambda venv_dir: call_order.append("create_venv")
        kwargs["run_pip_install"] = lambda pip, req: call_order.append("pip")
        kwargs["run_chown"] = lambda user, venv_dir: call_order.append("chown")
        setup_backend("/some/dir", **kwargs)
        assert call_order == ["create_venv", "pip", "chown"]

    def test_pip_before_chown(self):
        call_order = []
        kwargs = _passing_kwargs()
        kwargs["run_pip_install"] = lambda pip, req: call_order.append("pip")
        kwargs["run_chown"] = lambda user, venv_dir: call_order.append("chown")
        setup_backend("/some/dir", **kwargs)
        assert call_order.index("pip") < call_order.index("chown")

    def test_idempotent_rerun_skips_create_venv_only(self):
        call_order = []
        kwargs = _passing_kwargs(venv_present=True)
        kwargs["create_venv"] = lambda venv_dir: call_order.append("create_venv")
        kwargs["run_pip_install"] = lambda pip, req: call_order.append("pip")
        kwargs["run_chown"] = lambda user, venv_dir: call_order.append("chown")
        setup_backend("/some/dir", **kwargs)
        assert "create_venv" not in call_order
        assert call_order == ["pip", "chown"]


# ── TestBackendBoundaryProbe ──────────────────────────────────────────────────


class TestBackendBoundaryProbe:
    """F5/F10 boundary tests — binary absent surfaces MissingBinaryError;
    ensurepip stderr yields actionable VenvCreationError message.
    """

    def test_create_venv_raises_on_python3_absent(self):
        from unittest.mock import patch

        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("python3")):
            with pytest.raises(MissingBinaryError, match="python3"):
                _create_venv(Path("/some/dir/.venv"))

    def test_run_pip_install_raises_on_pip_absent(self):
        from unittest.mock import patch

        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("pip")):
            with pytest.raises(MissingBinaryError, match="pip"):
                _run_pip_install(Path("/some/.venv/bin/pip"), Path("/some/requirements.txt"))

    def test_run_chown_raises_on_chown_absent(self):
        from unittest.mock import patch

        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("chown")):
            with pytest.raises(MissingBinaryError, match="chown"):
                _run_chown("slop", Path("/some/.venv"))

    def test_create_venv_ensurepip_error_mentions_python3_venv(self):
        from unittest.mock import patch, MagicMock

        fake = MagicMock(
            returncode=1, stdout="", stderr="Error: Command '[...] -m ensurepip --upgrade' failed"
        )
        with patch("installer._run.subprocess.run", return_value=fake):
            with pytest.raises(VenvCreationError, match="python3-venv"):
                _create_venv(Path("/some/dir/.venv"))
