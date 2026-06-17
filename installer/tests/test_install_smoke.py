"""installer/tests/test_install_smoke.py — unit tests for the MS_SMOKE_STOP_AFTER hook.


The smoke hook in run_install_pipeline() reads MS_SMOKE_STOP_AFTER from the
environment and exits cleanly (return 0) after the named step completes.
These tests verify the hook contract without launching a Docker container.

Coverage:
  TestSmokeStopAfter  — env var absent/set; each stop point returns 0 and
                        skips downstream steps; unknown step name ignored
"""

from __future__ import annotations

import argparse


from installer.main import run_install_pipeline
from installer.smoke import SmokeTestResult


# ── Shared helpers (mirrors test_main.py) ────────────────────────────────────


class _FakeDistro:
    distro = "ubuntu"
    version = "24.04"


_FAKE_DISTRO = _FakeDistro()


def _ok_prereqs(*_args, **_kwargs):
    return []


def _make_args(**overrides):
    defaults = {
        "install_dir": "/opt/ms",
        "data_dir": "/var/lib/ms",
        "port": 8080,
        "install_docker": "yes",
        "force": False,
        "version_ref": None,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def _mocks(**overrides) -> dict:
    base = {
        "state_read": lambda p: None,
        "state_write": lambda s, p: None,
        "prereq_check": _ok_prereqs,
        "detect_os": lambda: _FAKE_DISTRO,
        "deps_install": lambda d: None,
        "docker_install": lambda cm: None,
        "user_install": lambda: None,
        "data_dir_install": lambda p: None,
        "fetch_repo": lambda d, version_ref=None: "v5.0.0",
        "setup_community_dir": lambda p: None,  # no-op; real mkdir+chown for prod
        "backend_setup": lambda d: None,
        "frontend_build": lambda d: None,
        "service_install": lambda d, dd: None,
        "smoke_test": lambda p, **kw: SmokeTestResult(
            predicate="all",
            passed=True,
            failure_shape="",
            operator_message="",
            diagnostic_command="",
        ),
        "resolve_hostname": lambda: "localhost",
        "post_install_write": lambda content, install_dir, **kw: None,
        "write_wrapper": lambda install_dir: None,
        "stdin_is_tty": lambda: True,
        "install_dir_exists": lambda p: False,
    }
    base.update(overrides)
    return base


# ── TestSmokeStopAfter ────────────────────────────────────────────────────────


class TestSmokeStopAfter:
    """Verify MS_SMOKE_STOP_AFTER hook contract in run_install_pipeline."""

    def test_no_env_var_runs_full_pipeline(self, monkeypatch):
        monkeypatch.delenv("MS_SMOKE_STOP_AFTER", raising=False)
        called = []
        result = run_install_pipeline(
            _make_args(),
            **_mocks(
                service_install=lambda d, dd: called.append("service"),
            ),
        )
        assert result == 0
        assert called == ["service"]

    def test_stop_after_deps_install_returns_zero(self, monkeypatch):
        monkeypatch.setenv("MS_SMOKE_STOP_AFTER", "deps_install")
        result = run_install_pipeline(_make_args(), **_mocks())
        assert result == 0

    def test_stop_after_deps_install_skips_docker(self, monkeypatch):
        monkeypatch.setenv("MS_SMOKE_STOP_AFTER", "deps_install")
        called = []
        run_install_pipeline(
            _make_args(),
            **_mocks(docker_install=lambda cm: called.append("docker")),
        )
        assert called == []

    def test_stop_after_docker_install_returns_zero(self, monkeypatch):
        monkeypatch.setenv("MS_SMOKE_STOP_AFTER", "docker_install")
        result = run_install_pipeline(_make_args(), **_mocks())
        assert result == 0

    def test_stop_after_docker_install_skips_user(self, monkeypatch):
        monkeypatch.setenv("MS_SMOKE_STOP_AFTER", "docker_install")
        called = []
        run_install_pipeline(
            _make_args(),
            **_mocks(user_install=lambda: called.append("user")),
        )
        assert called == []

    def test_stop_after_user_install_returns_zero(self, monkeypatch):
        monkeypatch.setenv("MS_SMOKE_STOP_AFTER", "user_install")
        result = run_install_pipeline(_make_args(), **_mocks())
        assert result == 0

    def test_stop_after_user_install_skips_data_dir(self, monkeypatch):
        monkeypatch.setenv("MS_SMOKE_STOP_AFTER", "user_install")
        called = []
        run_install_pipeline(
            _make_args(),
            **_mocks(data_dir_install=lambda p: called.append("data_dir")),
        )
        assert called == []

    def test_stop_after_data_dir_install_returns_zero(self, monkeypatch):
        monkeypatch.setenv("MS_SMOKE_STOP_AFTER", "data_dir_install")
        result = run_install_pipeline(_make_args(), **_mocks())
        assert result == 0

    def test_stop_after_data_dir_install_skips_fetch(self, monkeypatch):
        monkeypatch.setenv("MS_SMOKE_STOP_AFTER", "data_dir_install")
        called = []
        run_install_pipeline(
            _make_args(),
            **_mocks(fetch_repo=lambda d, version_ref=None: called.append("fetch") or "v5.0.0"),
        )
        assert called == []

    def test_stop_after_fetch_repo_returns_zero(self, monkeypatch):
        monkeypatch.setenv("MS_SMOKE_STOP_AFTER", "fetch_repo")
        result = run_install_pipeline(_make_args(), **_mocks())
        assert result == 0

    def test_stop_after_fetch_repo_skips_backend(self, monkeypatch):
        monkeypatch.setenv("MS_SMOKE_STOP_AFTER", "fetch_repo")
        called = []
        run_install_pipeline(
            _make_args(),
            **_mocks(backend_setup=lambda d: called.append("backend")),
        )
        assert called == []

    def test_stop_after_backend_setup_returns_zero(self, monkeypatch):
        monkeypatch.setenv("MS_SMOKE_STOP_AFTER", "backend_setup")
        result = run_install_pipeline(_make_args(), **_mocks())
        assert result == 0

    def test_stop_after_frontend_build_returns_zero(self, monkeypatch):
        monkeypatch.setenv("MS_SMOKE_STOP_AFTER", "frontend_build")
        result = run_install_pipeline(_make_args(), **_mocks())
        assert result == 0

    def test_unknown_stop_after_name_runs_full_pipeline(self, monkeypatch):
        monkeypatch.setenv("MS_SMOKE_STOP_AFTER", "nonexistent_step")
        called = []
        result = run_install_pipeline(
            _make_args(),
            **_mocks(service_install=lambda d, dd: called.append("service")),
        )
        assert result == 0
        assert called == ["service"]

    def test_stop_after_prints_smoke_stop_message(self, monkeypatch, capsys):
        monkeypatch.setenv("MS_SMOKE_STOP_AFTER", "deps_install")
        run_install_pipeline(_make_args(), **_mocks())
        out = capsys.readouterr().out
        assert "smoke-stop" in out
        assert "deps_install" in out
