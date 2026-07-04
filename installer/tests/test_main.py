"""installer/tests/test_main.py — orchestration tests for main.py.

Two test groups:

1. F1 regression tests (TestInstallHandoffF1, TestInstallBaselineWithoutPaths):
   install.sh ↔ main.py argparse contract.  These tests are the regression
   gate for the F1 handoff fix (install.sh passes resolved --install-dir and
   --data-dir explicitly).

2. Orchestration tests (Step 2.7.5): state machine, prereq, distro detection,
   pipeline ordering, state-file lifecycle, and consent resolution.
   All use the inject-kwargs pattern — no real modules are called.
"""

import argparse
import types
from pathlib import Path

import pytest

from installer.detect import UnsupportedDistroError
from installer.main import build_parser, run_install_pipeline
from installer.prereq import PrereqFinding
from installer.user import InstallUserMismatchError
from installer.smoke import SmokeTestResult
from installer.state import (
    StateFile,
    StateFileCorruptedError,
    StateFileNewerSchemaError,
)


# ── F1 regression tests (commit 6667713) ─────────────────────────────────────


class TestInstallHandoffF1:
    """install subcommand parses every flag install.sh passes after the F1 fix."""

    def _parse(self, *extra: str) -> argparse.Namespace:
        return build_parser().parse_args(
            ["install", "--install-dir=/opt/x", "--data-dir=/var/y", *extra]
        )

    def test_explicit_paths_parsed(self):
        args = self._parse()
        assert args.install_dir == "/opt/x"
        assert args.data_dir == "/var/y"

    def test_custom_paths_parsed(self):
        args = build_parser().parse_args(["install", "--install-dir=/tmp/x", "--data-dir=/tmp/y"])
        assert args.install_dir == "/tmp/x"
        assert args.data_dir == "/tmp/y"

    def test_install_docker_no(self):
        args = self._parse("--install-docker=no")
        assert args.install_docker == "no"

    def test_install_docker_yes(self):
        args = self._parse("--install-docker=yes")
        assert args.install_docker == "yes"

    def test_force_flag(self):
        args = self._parse("--force")
        assert args.force is True

    def test_full_handoff_set(self):
        """Simulate the complete _PY_ARGS array install.sh builds after F1."""
        args = build_parser().parse_args(
            [
                "install",
                "--install-dir=/opt/slop",
                "--data-dir=/var/lib/slop",
                "--install-docker=yes",
                "--force",
            ]
        )
        assert args.install_dir == "/opt/slop"
        assert args.data_dir == "/var/lib/slop"
        assert args.install_docker == "yes"
        assert args.force is True


class TestInstallBaselineWithoutPaths:
    """Tombstone: bare 'install' subcommand still yields None paths.

    install.sh no longer produces this invocation after F1.  The test
    confirms the fix lives in install.sh (explicit flags) rather than
    in argparse defaults — keeping main.py usable standalone.
    """

    def test_no_paths_gives_none(self):
        args = build_parser().parse_args(["install"])
        assert args.install_dir is None
        assert args.data_dir is None


# ── Orchestration test helpers ────────────────────────────────────────────────

_FAKE_DISTRO = types.SimpleNamespace(distro="debian", version="12")


def _make_args(**kw) -> argparse.Namespace:
    defaults = {
        "install_dir": "/opt/test-ms",
        "data_dir": "/var/test-ms",
        "install_docker": "yes",
        "force": False,
    }
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _make_state(phase: str = "installed", **kw) -> StateFile:
    defaults = {
        "schema_version": 1,
        "slop_version": "5.0.0",
        "phase": phase,
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:01:00Z" if phase == "installed" else None,
        "install_dir": "/opt/test-ms",
        "data_dir": "/var/test-ms",
        "install_user": "slop",
        "distro": "debian",
        "distro_version": "12",
        "port": 8080,
        "smoke_test_passed": (phase == "installed"),
    }
    defaults.update(kw)
    return StateFile(**defaults)


def _ok_prereqs(path, port):
    return [PrereqFinding(name="root", ok=True, remediation="")]


def _mocks(**overrides) -> dict:
    """Return a full inject-kwargs dict for run_install_pipeline.

    All module functions are no-ops by default (successful install path).
    Pass overrides to replace individual mocks.
    """
    base = {
        "state_read": lambda p: None,
        "state_write": lambda s, p: None,
        "prereq_check": _ok_prereqs,
        "detect_os": lambda: _FAKE_DISTRO,
        "check_user_attrs": lambda: None,
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
        "install_dir_exists": lambda p: False,  # S1 clean by default
        "remove_install_dir": lambda p: None,  # no-op; real shutil.rmtree for prod
        "stop_service": lambda: None,  # no-op; real systemctl stop for prod
    }
    base.update(overrides)
    return base


def _call_logger(log: list, name: str, return_val=None, raises=None):
    """Return a mock callable that appends *name* to *log* on each call."""

    def fn(*args, **kwargs):
        log.append(name)
        if raises is not None:
            raise raises
        return return_val

    return fn


# ── State machine tests ───────────────────────────────────────────────────────


class TestStateMachineS1:
    """S1: clean host (no state file, install dir absent) → proceeds."""

    def test_s1_proceeds_through_pipeline(self):
        log = []
        result = run_install_pipeline(
            _make_args(),
            **_mocks(
                deps_install=_call_logger(log, "deps"),
                docker_install=_call_logger(log, "docker"),
                user_install=_call_logger(log, "user"),
                fetch_repo=_call_logger(log, "fetch", return_val="v5.0.0"),
                backend_setup=_call_logger(log, "backend"),
                frontend_build=_call_logger(log, "frontend"),
                service_install=_call_logger(log, "service"),
            ),
        )
        assert result == 0
        # Steps 6 (backend) and 7 (frontend) run in parallel — their relative
        # order in the log is non-deterministic.  Assert the surrounding
        # sequence is correct and both are present.
        assert log[:4] == ["deps", "docker", "user", "fetch"]
        assert set(log[4:6]) == {"backend", "frontend"}
        assert log[6:] == ["service"]

    def test_s1_state_write_called_three_times(self):
        writes = []

        def record_write(s, p):
            writes.append(s.phase)

        result = run_install_pipeline(_make_args(), **_mocks(state_write=record_write))
        assert result == 0
        assert writes == ["installing", "installed", "installed"]


class TestStateMachineS2:
    """S2: phase=installed."""

    def test_s2_no_force_returns_zero_with_install_info(self, capsys):
        result = run_install_pipeline(
            _make_args(force=False),
            **_mocks(state_read=lambda p: _make_state(phase="installed")),
        )
        assert result == 0
        out = capsys.readouterr().out
        assert "already installed" in out

    def test_s2_no_force_does_not_call_pipeline(self):
        log = []
        run_install_pipeline(
            _make_args(force=False),
            **_mocks(
                state_read=lambda p: _make_state(phase="installed"),
                deps_install=_call_logger(log, "deps"),
            ),
        )
        assert log == []

    def test_s2_with_force_proceeds(self):
        log = []
        result = run_install_pipeline(
            _make_args(force=True),
            **_mocks(
                state_read=lambda p: _make_state(phase="installed"),
                deps_install=_call_logger(log, "deps"),
                fetch_repo=_call_logger(log, "fetch", return_val="v5.0.0"),
            ),
        )
        assert result == 0
        assert "deps" in log
        assert "fetch" in log


class TestStateMachineS3:
    """S3: phase=installing (previous install interrupted)."""

    def test_s3_no_force_refuses(self, capsys):
        result = run_install_pipeline(
            _make_args(force=False),
            **_mocks(state_read=lambda p: _make_state(phase="installing")),
        )
        assert result == 1
        out = capsys.readouterr().out
        assert "interrupted" in out

    def test_s3_no_force_does_not_call_pipeline(self):
        log = []
        run_install_pipeline(
            _make_args(force=False),
            **_mocks(
                state_read=lambda p: _make_state(phase="installing"),
                deps_install=_call_logger(log, "deps"),
            ),
        )
        assert log == []

    def test_s3_with_force_proceeds(self):
        log = []
        result = run_install_pipeline(
            _make_args(force=True),
            **_mocks(
                state_read=lambda p: _make_state(phase="installing"),
                deps_install=_call_logger(log, "deps"),
                fetch_repo=_call_logger(log, "fetch", return_val="v5.0.0"),
            ),
        )
        assert result == 0
        assert "deps" in log


class TestStateMachineS4:
    """S4: corrupted state file — unconditional refusal (force has no effect)."""

    def _corrupt_read(self, exc_type):
        def fn(p):
            raise exc_type("bad state")

        return fn

    def test_s4_corrupted_refuses(self, capsys):
        result = run_install_pipeline(
            _make_args(force=False),
            **_mocks(state_read=self._corrupt_read(StateFileCorruptedError)),
        )
        assert result == 1
        assert "unreadable" in capsys.readouterr().out

    def test_s4_corrupted_force_still_refuses(self):
        result = run_install_pipeline(
            _make_args(force=True),
            **_mocks(state_read=self._corrupt_read(StateFileCorruptedError)),
        )
        assert result == 1

    def test_s4_newer_schema_refuses(self):
        result = run_install_pipeline(
            _make_args(force=False),
            **_mocks(state_read=self._corrupt_read(StateFileNewerSchemaError)),
        )
        assert result == 1

    def test_s4_newer_schema_force_still_refuses(self):
        result = run_install_pipeline(
            _make_args(force=True),
            **_mocks(state_read=self._corrupt_read(StateFileNewerSchemaError)),
        )
        assert result == 1

    def test_s4_does_not_call_pipeline(self):
        log = []
        run_install_pipeline(
            _make_args(force=True),
            **_mocks(
                state_read=self._corrupt_read(StateFileCorruptedError),
                deps_install=_call_logger(log, "deps"),
            ),
        )
        assert log == []


class TestStateMachineS5:
    """S5: install dir present, no state file."""

    def test_s5_no_force_refuses(self, capsys):
        result = run_install_pipeline(
            _make_args(force=False),
            **_mocks(
                state_read=lambda p: None,
                install_dir_exists=lambda p: True,
            ),
        )
        assert result == 1
        out = capsys.readouterr().out
        assert "no slop v5 state file" in out

    def test_s5_no_force_does_not_call_pipeline(self):
        log = []
        run_install_pipeline(
            _make_args(force=False),
            **_mocks(
                state_read=lambda p: None,
                install_dir_exists=lambda p: True,
                deps_install=_call_logger(log, "deps"),
            ),
        )
        assert log == []

    def test_s5_with_force_proceeds(self):
        log = []
        result = run_install_pipeline(
            _make_args(force=True),
            **_mocks(
                state_read=lambda p: None,
                install_dir_exists=lambda p: True,
                deps_install=_call_logger(log, "deps"),
                fetch_repo=_call_logger(log, "fetch", return_val="v5.0.0"),
            ),
        )
        assert result == 0
        assert "deps" in log
        assert "fetch" in log


# ── Step 4.2 idempotent install tests ────────────────────────────────────────


class TestIdempotentInstall:
    """Step 4.2 — idempotent re-run and --force cleanup.

    4.2.a: state present + no --force → exit 0 no-op.
    4.2.b: state present + --force → remove_install_dir called, pipeline runs.
    Phase 1 resolutions: Interpretation A (narrow cleanup), S2b same as S2a,
    S3 no-force stays exit 1, S5 + --force proceeds with cleanup.
    """

    def _capture_remove(self):
        removed = []
        return lambda p: removed.append(p), removed

    # ── 4.2.b case 1: state present → no-op (S2a and S2b) ───────────────────

    def test_s2a_noop_exits_zero(self):
        result = run_install_pipeline(
            _make_args(force=False),
            **_mocks(state_read=lambda p: _make_state(phase="installed", smoke_test_passed=True)),
        )
        assert result == 0

    def test_s2a_noop_prints_install_info(self, capsys):
        run_install_pipeline(
            _make_args(force=False),
            **_mocks(state_read=lambda p: _make_state(phase="installed")),
        )
        assert "already installed" in capsys.readouterr().out

    def test_s2a_noop_does_not_call_pipeline(self):
        log = []
        run_install_pipeline(
            _make_args(force=False),
            **_mocks(
                state_read=lambda p: _make_state(phase="installed"),
                deps_install=_call_logger(log, "deps"),
            ),
        )
        assert log == []

    def test_s2b_noop_exits_zero(self):
        # Ambiguity 2 resolution: S2b (smoke failed) no-force is still a no-op
        result = run_install_pipeline(
            _make_args(force=False),
            **_mocks(state_read=lambda p: _make_state(phase="installed", smoke_test_passed=False)),
        )
        assert result == 0

    # ── 4.2.b case 2: state present + --force → cleanup + reinstall ─────────

    def test_s2a_force_calls_remove_install_dir(self):
        remove, removed = self._capture_remove()
        run_install_pipeline(
            _make_args(force=True),
            **_mocks(
                state_read=lambda p: _make_state(phase="installed"),
                remove_install_dir=remove,
            ),
        )
        assert Path("/opt/test-ms") in removed

    def test_s2a_force_removes_install_dir_not_data_dir(self):
        # Spec: --force does NOT touch data dir
        remove, removed = self._capture_remove()
        run_install_pipeline(
            _make_args(force=True),
            **_mocks(
                state_read=lambda p: _make_state(phase="installed"),
                remove_install_dir=remove,
            ),
        )
        assert removed == [Path("/opt/test-ms")]

    def test_s2a_force_proceeds_with_pipeline(self):
        log = []
        result = run_install_pipeline(
            _make_args(force=True),
            **_mocks(
                state_read=lambda p: _make_state(phase="installed"),
                deps_install=_call_logger(log, "deps"),
                fetch_repo=_call_logger(log, "fetch", return_val="v5.0.0"),
            ),
        )
        assert result == 0
        assert "deps" in log
        assert "fetch" in log

    def test_s2b_force_calls_remove_install_dir(self):
        # Ambiguity 2 resolution: S2b + --force same cleanup path as S2a
        remove, removed = self._capture_remove()
        run_install_pipeline(
            _make_args(force=True),
            **_mocks(
                state_read=lambda p: _make_state(phase="installed", smoke_test_passed=False),
                remove_install_dir=remove,
            ),
        )
        assert Path("/opt/test-ms") in removed

    def test_s3_force_calls_remove_install_dir(self):
        # Question A resolution: S3 (interrupted) + --force → cleanup + install
        remove, removed = self._capture_remove()
        result = run_install_pipeline(
            _make_args(force=True),
            **_mocks(
                state_read=lambda p: _make_state(phase="installing"),
                remove_install_dir=remove,
            ),
        )
        assert result == 0
        assert Path("/opt/test-ms") in removed

    def test_force_stop_service_called_before_prereq(self):
        # F-05-B: stop_service must run before prereq_check so port is free.
        log: list = []
        run_install_pipeline(
            _make_args(force=True),
            **_mocks(
                state_read=lambda p: _make_state(phase="installed"),
                stop_service=_call_logger(log, "stop"),
                prereq_check=lambda p, port: log.append("prereq") or _ok_prereqs(p, port),
            ),
        )
        assert log.index("stop") < log.index("prereq")

    def test_force_stop_service_not_called_on_clean_path(self):
        # stop_service must NOT run on a clean (non-force) install.
        stopped: list = []
        run_install_pipeline(
            _make_args(force=False),
            **_mocks(stop_service=lambda: stopped.append(True)),
        )
        assert stopped == []

    def test_force_preflight_fail_does_not_remove_install_dir(self):
        # F-05-A: if pre-flight fails, the old install dir must be preserved.
        removed: list = []

        def failing_prereqs(path, port):
            return [PrereqFinding("port 8080 (backend)", False, "port in use")]

        run_install_pipeline(
            _make_args(force=True),
            **_mocks(
                state_read=lambda p: _make_state(phase="installed"),
                prereq_check=failing_prereqs,
                remove_install_dir=lambda p: removed.append(p),
            ),
        )
        assert removed == []

    def test_force_remove_install_dir_called_after_prereq(self):
        # F-05-A: removal must happen after pre-flight, not before.
        log: list = []
        run_install_pipeline(
            _make_args(force=True),
            **_mocks(
                state_read=lambda p: _make_state(phase="installed"),
                prereq_check=lambda p, port: log.append("prereq") or _ok_prereqs(p, port),
                remove_install_dir=lambda p: log.append("remove"),
            ),
        )
        assert log.index("prereq") < log.index("remove")

    # ── 4.2.b case 3: state absent + install dir present → refuse ────────────

    def test_s5_no_force_refuses(self):
        result = run_install_pipeline(
            _make_args(force=False),
            **_mocks(
                state_read=lambda p: None,
                install_dir_exists=lambda p: True,
            ),
        )
        assert result == 1

    # ── S5 + --force: Question B resolution — cleanup + proceed ─────────────

    def test_s5_force_calls_remove_install_dir(self):
        # Question B resolution: S5 + --force removes orphaned install dir + installs
        remove, removed = self._capture_remove()
        result = run_install_pipeline(
            _make_args(force=True),
            **_mocks(
                state_read=lambda p: None,
                install_dir_exists=lambda p: True,
                remove_install_dir=remove,
            ),
        )
        assert result == 0
        assert Path("/opt/test-ms") in removed


# ── Prereq failure tests ──────────────────────────────────────────────────────


class TestPrereqFailure:
    """Prereq failures cause exit before any module call."""

    def test_prereq_fail_returns_nonzero(self):
        def failing_prereqs(path, port):
            return [PrereqFinding("root", False, "must be root")]

        result = run_install_pipeline(_make_args(), **_mocks(prereq_check=failing_prereqs))
        assert result == 1

    def test_prereq_fail_no_module_called(self):
        log = []

        def failing_prereqs(path, port):
            return [PrereqFinding("root", False, "must be root")]

        run_install_pipeline(
            _make_args(),
            **_mocks(
                prereq_check=failing_prereqs,
                deps_install=_call_logger(log, "deps"),
                detect_os=_call_logger(log, "detect", return_val=_FAKE_DISTRO),
            ),
        )
        assert log == []

    def test_prereq_fail_message_names_finding(self, capsys):
        def failing_prereqs(path, port):
            return [PrereqFinding("disk space", False, "free up 5GiB")]

        run_install_pipeline(_make_args(), **_mocks(prereq_check=failing_prereqs))
        out = capsys.readouterr().out
        assert "disk space" in out

    def test_all_prereqs_ok_proceeds(self):
        log = []
        run_install_pipeline(
            _make_args(),
            **_mocks(
                prereq_check=lambda p, port: [
                    PrereqFinding("kernel", True, ""),
                    PrereqFinding("root", True, ""),
                ],
                deps_install=_call_logger(log, "deps"),
                fetch_repo=_call_logger(log, "fetch", return_val="v5.0.0"),
            ),
        )
        assert "deps" in log


# ── Distro detection failure tests ────────────────────────────────────────────


class TestDistroDetectionFailure:
    """UnsupportedDistroError causes exit before any side effect."""

    def _unsupported(self):
        raise UnsupportedDistroError("fedora not supported")

    def test_unsupported_distro_returns_nonzero(self):
        result = run_install_pipeline(_make_args(), **_mocks(detect_os=self._unsupported))
        assert result == 1

    def test_unsupported_distro_no_pipeline_call(self):
        log = []
        run_install_pipeline(
            _make_args(),
            **_mocks(
                detect_os=self._unsupported,
                deps_install=_call_logger(log, "deps"),
            ),
        )
        assert log == []

    def test_unsupported_distro_no_state_write(self):
        writes = []
        run_install_pipeline(
            _make_args(),
            **_mocks(
                detect_os=self._unsupported,
                state_write=lambda s, p: writes.append(s.phase),
            ),
        )
        assert writes == []


# ── User attribute pre-flight tests (Exercise 6.3 fix) ───────────────────────


class TestUserAttrsPreflight:
    """InstallUserMismatchError fires before any state write (Ex 6.3 fix)."""

    def _mismatch(self):
        raise InstallUserMismatchError("user `slop` has wrong shell '/bin/bash'")

    def test_mismatch_returns_nonzero(self):
        result = run_install_pipeline(_make_args(), **_mocks(check_user_attrs=self._mismatch))
        assert result == 1

    def test_mismatch_no_state_write(self):
        writes = []
        run_install_pipeline(
            _make_args(),
            **_mocks(
                check_user_attrs=self._mismatch,
                state_write=lambda s, p: writes.append(s.phase),
            ),
        )
        assert writes == []

    def test_mismatch_no_pipeline_call(self):
        log = []
        run_install_pipeline(
            _make_args(),
            **_mocks(
                check_user_attrs=self._mismatch,
                deps_install=_call_logger(log, "deps"),
            ),
        )
        assert log == []

    def test_correct_attrs_proceeds(self):
        result = run_install_pipeline(_make_args(), **_mocks(check_user_attrs=lambda: None))
        assert result == 0


# ── Pipeline ordering tests ───────────────────────────────────────────────────


class TestPipelineOrder:
    """Successful install calls modules in the correct order."""

    def test_full_pipeline_order(self):
        log = []
        result = run_install_pipeline(
            _make_args(),
            **_mocks(
                deps_install=_call_logger(log, "deps"),
                docker_install=_call_logger(log, "docker"),
                user_install=_call_logger(log, "user"),
                fetch_repo=_call_logger(log, "fetch", return_val="v5.0.0"),
                backend_setup=_call_logger(log, "backend"),
                frontend_build=_call_logger(log, "frontend"),
                service_install=_call_logger(log, "service"),
            ),
        )
        assert result == 0
        # Steps 6 (backend) and 7 (frontend) run in parallel — their relative
        # order in the log is non-deterministic.  Assert the surrounding
        # sequence is correct and both are present.
        assert log[:4] == ["deps", "docker", "user", "fetch"]
        assert set(log[4:6]) == {"backend", "frontend"}
        assert log[6:] == ["service"]

    @pytest.mark.parametrize(
        "fail_at,expected_called,not_called",
        [
            ("deps", [], ["docker", "user", "fetch", "backend", "frontend", "service"]),
            ("docker", ["deps"], ["user", "fetch", "backend", "frontend", "service"]),
            ("user", ["deps", "docker"], ["fetch", "backend", "frontend", "service"]),
            ("fetch", ["deps", "docker", "user"], ["backend", "frontend", "service"]),
            # backend and frontend run in parallel: when backend fails, frontend still
            # runs to completion (both threads are joined before the error propagates).
            # The critical invariant is that "service" is NOT called — not that
            # "frontend" is skipped.
            ("backend", ["deps", "docker", "user", "fetch"], ["service"]),
            ("frontend", ["deps", "docker", "user", "fetch", "backend"], ["service"]),
        ],
    )
    def test_pipeline_halts_at_failing_module(self, fail_at, expected_called, not_called):
        log = []

        def make_mock(name):
            exc = RuntimeError(f"{name} failed") if name == fail_at else None
            rv = "v5.0.0" if name == "fetch" else None
            return _call_logger(log, name, return_val=rv, raises=exc)

        result = run_install_pipeline(
            _make_args(),
            **_mocks(
                deps_install=make_mock("deps"),
                docker_install=make_mock("docker"),
                user_install=make_mock("user"),
                fetch_repo=make_mock("fetch"),
                backend_setup=make_mock("backend"),
                frontend_build=make_mock("frontend"),
                service_install=make_mock("service"),
            ),
        )
        assert result == 1
        for name in expected_called:
            assert name in log, f"expected {name!r} to have been called"
        for name in not_called:
            assert name not in log, f"expected {name!r} NOT to have been called"


# ── State file lifecycle tests ────────────────────────────────────────────────


class TestStateFileLifecycle:
    """Pre-write / post-write ordering and failure preservation."""

    def test_pre_write_before_first_module_call(self):
        log = []

        def record_write(s, p):
            log.append(("write", s.phase))

        def record_deps(d):
            log.append(("deps", None))

        result = run_install_pipeline(
            _make_args(),
            **_mocks(
                state_write=record_write,
                deps_install=record_deps,
                fetch_repo=lambda d, version_ref=None: "v5.0.0",
            ),
        )
        assert result == 0
        # pre-write ("installing") must come before the first module call
        first_write_idx = next(i for i, x in enumerate(log) if x[0] == "write")
        deps_idx = next(i for i, x in enumerate(log) if x[0] == "deps")
        assert first_write_idx < deps_idx
        assert log[first_write_idx] == ("write", "installing")

    def test_post_write_after_service_install(self):
        log = []

        def record_write(s, p):
            log.append(("write", s.phase))

        def record_service(d, dd):
            log.append(("service", None))

        result = run_install_pipeline(
            _make_args(),
            **_mocks(
                state_write=record_write,
                service_install=record_service,
                fetch_repo=lambda d, version_ref=None: "v5.0.0",
            ),
        )
        assert result == 0
        service_idx = next(i for i, x in enumerate(log) if x[0] == "service")
        post_write_entries = [i for i, x in enumerate(log) if x == ("write", "installed")]
        assert post_write_entries, "post-write (installed) never happened"
        assert post_write_entries[0] > service_idx

    def test_module_failure_leaves_state_at_installing(self):
        writes = []

        def record_write(s, p):
            writes.append(s.phase)

        def failing_deps(d):
            raise RuntimeError("apt failed")

        result = run_install_pipeline(
            _make_args(),
            **_mocks(
                state_write=record_write,
                deps_install=failing_deps,
            ),
        )
        assert result == 1
        assert writes == ["installing"]  # pre-write only; no post-write

    def test_successful_install_pre_and_post_write(self):
        writes = []

        def record_write(s, p):
            writes.append(s.phase)

        result = run_install_pipeline(
            _make_args(),
            **_mocks(
                state_write=record_write,
                fetch_repo=lambda d, version_ref=None: "v5.0.0",
            ),
        )
        assert result == 0
        assert writes == ["installing", "installed", "installed"]


# ── Consent resolution tests ──────────────────────────────────────────────────


class TestConsentResolution:
    """--install-docker flag and TTY state map correctly to consent_mode."""

    def _capture_consent(self):
        captured = []

        def mock_docker(cm):
            captured.append(cm)

        return mock_docker, captured

    def test_install_docker_yes_gives_yes(self):
        mock, captured = self._capture_consent()
        run_install_pipeline(
            _make_args(install_docker="yes"),
            **_mocks(docker_install=mock, fetch_repo=lambda d, version_ref=None: "v5.0.0"),
        )
        assert captured == ["yes"]

    def test_install_docker_no_gives_no(self):
        mock, captured = self._capture_consent()
        run_install_pipeline(
            _make_args(install_docker="no"),
            **_mocks(docker_install=mock, fetch_repo=lambda d, version_ref=None: "v5.0.0"),
        )
        assert captured == ["no"]

    def test_unset_tty_gives_none_interactive(self):
        mock, captured = self._capture_consent()
        run_install_pipeline(
            _make_args(install_docker=None),
            **_mocks(
                docker_install=mock,
                stdin_is_tty=lambda: True,
                fetch_repo=lambda d, version_ref=None: "v5.0.0",
            ),
        )
        assert captured == [None]

    def test_unset_pipe_raises(self):
        result = run_install_pipeline(
            _make_args(install_docker=None),
            **_mocks(stdin_is_tty=lambda: False),
        )
        assert result == 1

    def test_unset_pipe_no_module_call(self):
        log = []
        run_install_pipeline(
            _make_args(install_docker=None),
            **_mocks(
                stdin_is_tty=lambda: False,
                deps_install=_call_logger(log, "deps"),
            ),
        )
        assert log == []


# ── Module-form import regression (Step 2.8 fix) ─────────────────────────────


class TestModuleFormImport:
    """Regression gate: python3 -m installer.main resolves all package imports.

    Protects against regressing to `exec python3 installer/main.py` which
    fails with ModuleNotFoundError because the repo root is not in sys.path.
    The fix (Step 2.8): `cd "$_REPO_DIR" && exec python3 -m installer.main`.
    """

    def test_module_form_help_exits_zero(self):
        import subprocess
        import sys
        from pathlib import Path

        repo_root = Path(__file__).parent.parent.parent
        result = subprocess.run(
            [sys.executable, "-m", "installer.main", "--help"],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert "ModuleNotFoundError" not in result.stderr, result.stderr
        assert result.returncode == 0, f"--help exited {result.returncode}: {result.stderr}"
        assert "install" in result.stdout


# ── --version-ref flag wiring (Step 2.8) ──────────────────────────────────────


class TestVersionRefArgParsing:
    """--version-ref is parsed and forwarded to fetch_repo (Step 2.8 wiring)."""

    def _parse(self, *extra: str) -> argparse.Namespace:
        return build_parser().parse_args(
            ["install", "--install-dir=/opt/x", "--data-dir=/var/y", *extra]
        )

    def test_version_ref_flag_parsed(self):
        args = self._parse("--version-ref=v5.0.0-pre0")
        assert args.version_ref == "v5.0.0-pre0"

    def test_version_ref_defaults_to_none(self):
        args = self._parse()
        assert args.version_ref is None

    def test_version_ref_forwarded_to_fetch_repo(self):
        calls = []

        def capture_fetch(d, version_ref=None):
            calls.append(version_ref)
            return "v5.0.0-pre0"

        run_install_pipeline(
            _make_args(version_ref="v5.0.0-pre0"),
            **_mocks(fetch_repo=capture_fetch),
        )
        assert calls == ["v5.0.0-pre0"]

    def test_none_version_ref_forwarded_when_unset(self):
        calls = []

        def capture_fetch(d, version_ref=None):
            calls.append(version_ref)
            return "v5.0.0"

        run_install_pipeline(
            _make_args(),
            **_mocks(fetch_repo=capture_fetch),
        )
        assert calls == [None]


# ── TestPipelineFailureMessage ────────────────────────────────────────────────


class TestPipelineFailureMessage:
    """F15: pipeline exception output names the exception class."""

    def test_failure_message_includes_exception_class_name(self, capsys):
        class _FakeInstallError(Exception):
            pass

        def fail_backend(d):
            raise _FakeInstallError("venv creation failed")

        run_install_pipeline(_make_args(), **_mocks(backend_setup=fail_backend))
        out = capsys.readouterr().out
        assert "_FakeInstallError" in out

    def test_failure_message_includes_exception_text(self, capsys):
        def fail_backend(d):
            raise RuntimeError("disk full")

        run_install_pipeline(_make_args(), **_mocks(backend_setup=fail_backend))
        out = capsys.readouterr().out
        assert "disk full" in out


# ── TestDataDirPipelineWiring ─────────────────────────────────────────────────


class TestDataDirPipelineWiring:
    """F16 regression gate: ensure_data_dir must be called and correctly ordered.

    These tests catch the 'module exists but not wired' failure class — the
    state where installer/data_dir.py is created but ensure_data_dir is never
    called in run_install_pipeline.  Ordering tests also catch misplacement
    (e.g. wired after service_install instead of before it).
    """

    def test_data_dir_install_called_during_pipeline(self):
        calls = []
        result = run_install_pipeline(
            _make_args(),
            **_mocks(data_dir_install=lambda p: calls.append(p)),
        )
        assert result == 0
        assert len(calls) == 1, (
            "ensure_data_dir was not called — check wiring in run_install_pipeline"
        )

    def test_data_dir_install_called_with_data_dir_path(self):
        from pathlib import Path

        calls = []
        result = run_install_pipeline(
            _make_args(data_dir="/var/test-data"),
            **_mocks(data_dir_install=lambda p: calls.append(p)),
        )
        assert result == 0
        assert calls == [Path("/var/test-data")]

    def test_data_dir_called_after_user_install(self):
        log = []
        run_install_pipeline(
            _make_args(),
            **_mocks(
                user_install=_call_logger(log, "user"),
                data_dir_install=_call_logger(log, "data_dir"),
            ),
        )
        assert log.index("user") < log.index("data_dir")

    def test_data_dir_called_before_service_install(self):
        log = []
        run_install_pipeline(
            _make_args(),
            **_mocks(
                data_dir_install=_call_logger(log, "data_dir"),
                service_install=_call_logger(log, "service"),
            ),
        )
        assert log.index("data_dir") < log.index("service")

    def test_data_dir_called_between_user_and_service(self):
        log = []
        run_install_pipeline(
            _make_args(),
            **_mocks(
                user_install=_call_logger(log, "user"),
                data_dir_install=_call_logger(log, "data_dir"),
                service_install=_call_logger(log, "service"),
            ),
        )
        assert log.index("user") < log.index("data_dir") < log.index("service")
