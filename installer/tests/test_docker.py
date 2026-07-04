"""installer/tests/test_docker.py — unit tests for installer/docker.py.

All I/O is mocked via ensure_docker keyword-only injection.  No real Docker
calls, no real curl invocations, no real TTY prompts.

Coverage per DEPENDENCIES.md §Docker Handling consent resolution table:

  TestParseDockerVersion     — version-string parsing edge cases
  TestEnsureDockerD2Present  — D2 (present, >= 24.0) — no-op for all consent modes
  TestEnsureDockerD4Daemon   — D4 (daemon unreachable) — always DockerDaemonError
  TestEnsureDockerD1Absent   — D1 (absent) x consent yes/no/interactive
  TestEnsureDockerD3TooOld   — D3 (present, < 24.0) x consent yes/no/interactive
  TestEnsureDockerPostInstall — post-install verification failure paths
"""

from __future__ import annotations


import pytest

from installer._run import MissingBinaryError
from installer.docker import (
    DockerDaemonError,
    DockerInstallFailedError,
    DockerMissingError,
    DockerTooOldError,
    DockerVersionUnparseableError,
    _parse_docker_version,
    _run_docker_install,
    _verify_docker_post_install,
    ensure_docker,
)

# ── Shared test helpers ───────────────────────────────────────────────────────


def _noop_install():
    pass


def _noop_verify():
    pass


def _passing_kwargs(
    has_cmd: bool = True,
    version: str | None = "27.0.3",
    install_fn=_noop_install,
    verify_fn=_noop_verify,
    prompt_answer: bool = True,
) -> dict:
    """Return ensure_docker kwargs for a host where Docker is present and ok (D2)."""
    return {
        "has_docker_cmd": lambda: has_cmd,
        "get_docker_version": lambda: version,
        "run_docker_install": install_fn,
        "verify_post_install": verify_fn,
        "prompt_user": lambda q: prompt_answer,
    }


def _d1_kwargs(**overrides) -> dict:
    """D1: Docker absent."""
    install_fn = overrides.pop("install_fn", _noop_install)
    verify_fn = overrides.pop("verify_fn", _noop_verify)
    base = _passing_kwargs(has_cmd=False, version=None, install_fn=install_fn, verify_fn=verify_fn)
    base.update(overrides)
    return base


def _d3_kwargs(version: str = "23.0.1", **overrides) -> dict:
    """D3: Docker present, version < 24.0."""
    install_fn = overrides.pop("install_fn", _noop_install)
    verify_fn = overrides.pop("verify_fn", _noop_verify)
    base = _passing_kwargs(
        has_cmd=True, version=version, install_fn=install_fn, verify_fn=verify_fn
    )
    base.update(overrides)
    return base


def _d4_kwargs(**overrides) -> dict:
    """D4: Docker installed but daemon unreachable."""
    install_fn = overrides.pop("install_fn", _noop_install)
    verify_fn = overrides.pop("verify_fn", _noop_verify)
    base = _passing_kwargs(has_cmd=True, version=None, install_fn=install_fn, verify_fn=verify_fn)
    base.update(overrides)
    return base


# ── TestParseDockerVersion ────────────────────────────────────────────────────


class TestParseDockerVersion:
    def test_current_stable(self):
        assert _parse_docker_version("27.0.3") == (27, 0)

    def test_at_floor(self):
        assert _parse_docker_version("24.0.0") == (24, 0)

    def test_below_floor(self):
        assert _parse_docker_version("23.0.1") == (23, 0)

    def test_ce_suffix_stripped(self):
        assert _parse_docker_version("27.0.3-ce") == (27, 0)

    def test_unparseable_raises(self):
        with pytest.raises(DockerVersionUnparseableError):
            _parse_docker_version("not-a-version")


# ── TestEnsureDockerD2Present ─────────────────────────────────────────────────


class TestEnsureDockerD2Present:
    """D2: Docker present and version >= 24.0 — should be a no-op regardless of consent."""

    def test_no_op_consent_yes(self):
        installs = []
        kwargs = _passing_kwargs(install_fn=lambda: installs.append(1))
        ensure_docker("yes", **kwargs)
        assert len(installs) == 0

    def test_no_op_consent_no(self):
        installs = []
        kwargs = _passing_kwargs(install_fn=lambda: installs.append(1))
        ensure_docker("no", **kwargs)
        assert len(installs) == 0

    def test_no_op_consent_unset(self):
        installs = []
        prompts = []
        kwargs = _passing_kwargs(
            install_fn=lambda: installs.append(1),
            prompt_answer=True,
        )
        kwargs["prompt_user"] = lambda q: prompts.append(q) or False
        ensure_docker(None, **kwargs)
        assert len(installs) == 0
        assert len(prompts) == 0

    def test_at_exact_floor(self):
        installs = []
        kwargs = _passing_kwargs(version="24.0.0", install_fn=lambda: installs.append(1))
        ensure_docker("yes", **kwargs)
        assert len(installs) == 0


# ── TestEnsureDockerD4Daemon ──────────────────────────────────────────────────


class TestEnsureDockerD4Daemon:
    """D4: Docker installed but daemon unreachable — always DockerDaemonError."""

    def test_daemon_error_consent_yes(self):
        with pytest.raises(DockerDaemonError):
            ensure_docker("yes", **_d4_kwargs())

    def test_daemon_error_consent_no(self):
        with pytest.raises(DockerDaemonError):
            ensure_docker("no", **_d4_kwargs())

    def test_daemon_error_consent_unset(self):
        with pytest.raises(DockerDaemonError):
            ensure_docker(None, **_d4_kwargs())

    def test_daemon_error_message_contains_systemctl(self):
        with pytest.raises(DockerDaemonError, match="systemctl start docker"):
            ensure_docker("yes", **_d4_kwargs())


# ── TestEnsureDockerD1Absent ──────────────────────────────────────────────────


class TestEnsureDockerD1Absent:
    """D1: Docker absent — behavior depends on consent_mode."""

    def test_consent_yes_installs(self):
        installs = []
        verifies = []
        kwargs = _d1_kwargs(
            install_fn=lambda: installs.append(1),
            verify_fn=lambda: verifies.append(1),
        )
        ensure_docker("yes", **kwargs)
        assert len(installs) == 1
        assert len(verifies) == 1

    def test_consent_no_raises_missing(self):
        with pytest.raises(DockerMissingError):
            ensure_docker("no", **_d1_kwargs())

    def test_consent_no_does_not_install(self):
        installs = []
        kwargs = _d1_kwargs(install_fn=lambda: installs.append(1))
        with pytest.raises(DockerMissingError):
            ensure_docker("no", **kwargs)
        assert len(installs) == 0

    def test_interactive_yes_installs(self):
        installs = []
        verifies = []
        kwargs = _d1_kwargs(
            install_fn=lambda: installs.append(1),
            verify_fn=lambda: verifies.append(1),
        )
        kwargs["prompt_user"] = lambda q: True
        ensure_docker(None, **kwargs)
        assert len(installs) == 1
        assert len(verifies) == 1

    def test_interactive_no_raises_missing(self):
        kwargs = _d1_kwargs()
        kwargs["prompt_user"] = lambda q: False
        with pytest.raises(DockerMissingError):
            ensure_docker(None, **kwargs)

    def test_interactive_prompt_mentions_install(self):
        prompts = []
        kwargs = _d1_kwargs()
        kwargs["prompt_user"] = lambda q: prompts.append(q) or False
        with pytest.raises(DockerMissingError):
            ensure_docker(None, **kwargs)
        assert len(prompts) == 1
        assert "not installed" in prompts[0].lower() or "Install" in prompts[0]

    def test_missing_error_message_has_remediation(self):
        with pytest.raises(DockerMissingError, match=r"get.docker.com"):
            ensure_docker("no", **_d1_kwargs())


# ── TestEnsureDockerD3TooOld ──────────────────────────────────────────────────


class TestEnsureDockerD3TooOld:
    """D3: Docker present, version < 24.0 — behavior depends on consent_mode."""

    def test_consent_yes_upgrades(self):
        installs = []
        verifies = []
        kwargs = _d3_kwargs(
            install_fn=lambda: installs.append(1),
            verify_fn=lambda: verifies.append(1),
        )
        ensure_docker("yes", **kwargs)
        assert len(installs) == 1
        assert len(verifies) == 1

    def test_consent_no_raises_too_old(self):
        with pytest.raises(DockerTooOldError):
            ensure_docker("no", **_d3_kwargs())

    def test_consent_no_does_not_install(self):
        installs = []
        kwargs = _d3_kwargs(install_fn=lambda: installs.append(1))
        with pytest.raises(DockerTooOldError):
            ensure_docker("no", **kwargs)
        assert len(installs) == 0

    def test_too_old_error_names_version(self):
        with pytest.raises(DockerTooOldError, match=r"23.0.1"):
            ensure_docker("no", **_d3_kwargs(version="23.0.1"))

    def test_interactive_yes_upgrades(self):
        installs = []
        kwargs = _d3_kwargs(install_fn=lambda: installs.append(1))
        kwargs["prompt_user"] = lambda q: True
        ensure_docker(None, **kwargs)
        assert len(installs) == 1

    def test_interactive_no_raises_too_old(self):
        kwargs = _d3_kwargs()
        kwargs["prompt_user"] = lambda q: False
        with pytest.raises(DockerTooOldError):
            ensure_docker(None, **kwargs)

    def test_interactive_prompt_mentions_upgrade(self):
        prompts = []
        kwargs = _d3_kwargs(version="20.10.5")
        kwargs["prompt_user"] = lambda q: prompts.append(q) or False
        with pytest.raises(DockerTooOldError):
            ensure_docker(None, **kwargs)
        assert len(prompts) == 1
        assert "20.10.5" in prompts[0]

    def test_just_below_floor(self):
        with pytest.raises(DockerTooOldError):
            ensure_docker("no", **_d3_kwargs(version="23.9.9"))


# ── TestEnsureDockerPostInstall ───────────────────────────────────────────────


class TestEnsureDockerPostInstall:
    """Verify that install errors surface DockerInstallFailedError."""

    def test_install_failure_raises(self):
        def fail_install():
            raise DockerInstallFailedError("get.docker.com failed")

        kwargs = _d1_kwargs(install_fn=fail_install)
        with pytest.raises(DockerInstallFailedError):
            ensure_docker("yes", **kwargs)

    def test_post_install_verify_called_after_install(self):
        call_order = []
        kwargs = _d1_kwargs(
            install_fn=lambda: call_order.append("install"),
            verify_fn=lambda: call_order.append("verify"),
        )
        ensure_docker("yes", **kwargs)
        assert call_order == ["install", "verify"]

    def test_post_install_verify_failure_raises(self):
        def fail_verify():
            raise DockerInstallFailedError("post-install check failed")

        kwargs = _d1_kwargs(verify_fn=fail_verify)
        with pytest.raises(DockerInstallFailedError):
            ensure_docker("yes", **kwargs)

    def test_verify_not_called_when_consent_no(self):
        verifies = []
        kwargs = _d1_kwargs(verify_fn=lambda: verifies.append(1))
        with pytest.raises(DockerMissingError):
            ensure_docker("no", **kwargs)
        assert len(verifies) == 0


# ── TestGetDockerVersionProbe ─────────────────────────────────────────────────


class TestGetDockerVersionProbe:
    """_get_docker_version returns None when the docker binary is absent."""

    def test_returns_none_on_file_not_found(self):
        from unittest.mock import patch
        from installer.docker import _get_docker_version

        # Patch at installer._run (docker.py no longer imports subprocess directly).
        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("docker")):
            assert _get_docker_version() is None


# ── TestVerifyDockerPostInstallProbe ──────────────────────────────────────────


class TestVerifyDockerPostInstallProbe:
    """Boundary tests for _verify_docker_post_install compose-plugin probe (F3/F7 migration)."""

    def test_compose_filenotfound_raises_install_failed(self):
        # Patch _get_docker_version to return valid version (bypass its subprocess call),
        # then let the compose run_required raise MissingBinaryError → DockerInstallFailedError.
        from unittest.mock import patch

        with patch("installer.docker._get_docker_version", return_value="27.0.3"):
            with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("docker")):
                with pytest.raises(DockerInstallFailedError, match="compose plugin"):
                    _verify_docker_post_install()

    def test_compose_nonzero_returncode_raises_install_failed(self):
        from unittest.mock import patch, MagicMock

        fake_result = MagicMock(returncode=1, stdout="", stderr="error")
        with patch("installer.docker._get_docker_version", return_value="27.0.3"):
            with patch("installer._run.subprocess.run", return_value=fake_result):
                with pytest.raises(DockerInstallFailedError):
                    _verify_docker_post_install()

    def test_compose_success_does_not_raise(self):
        from unittest.mock import patch, MagicMock

        fake_result = MagicMock(returncode=0, stdout="v2.27.0", stderr="")
        with patch("installer.docker._get_docker_version", return_value="27.0.3"):
            with patch("installer._run.subprocess.run", return_value=fake_result):
                _verify_docker_post_install()  # must not raise


# ── TestRunDockerInstallProbe ─────────────────────────────────────────────────


class TestRunDockerInstallProbe:
    """F6 boundary tests for _run_docker_install curl pre-condition check."""

    def test_raises_when_curl_absent(self):
        from unittest.mock import patch

        with patch("installer.docker.shutil.which", return_value=None):
            with pytest.raises(DockerInstallFailedError, match="curl is required"):
                _run_docker_install()

    def test_bash_filenotfound_raises_missing_binary_error(self):
        from unittest.mock import patch

        with patch("installer.docker.shutil.which", return_value="/usr/bin/curl"):
            with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("bash")):
                with pytest.raises(MissingBinaryError, match="bash"):
                    _run_docker_install()

    def test_nonzero_returncode_raises_install_failed(self):
        from unittest.mock import patch, MagicMock

        fake = MagicMock(returncode=1, stdout="", stderr="installation failed")
        with patch("installer.docker.shutil.which", return_value="/usr/bin/curl"):
            with patch("installer._run.subprocess.run", return_value=fake):
                with pytest.raises(DockerInstallFailedError):
                    _run_docker_install()
