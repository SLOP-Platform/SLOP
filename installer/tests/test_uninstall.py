"""installer/tests/test_uninstall.py — tests for installer/uninstall.py.

Covers ADR 0017 §A refusal logic (§A.2/§A.3/§A.3.5/§A.4/§A.5),
§A.6/§A.6.5 carve-outs, §B pipeline U-predicates, §C clean structure,
verify_removed() U1-U7 per §A.7/§B.2, and class predictions A-S.
"""

from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar
from unittest.mock import patch


from installer.state import StateFile, StateFileCorruptedError, StateFileNewerSchemaError
from installer.uninstall import (
    _check_pipe_refusal,
    _print_clean_output,
    _prompt_confirm,
    _read_state,
    _run_removal_pipeline,
    run_clean,
    run_purge,
    run_uninstall,
    verify_removed,
)

# ── Shared factories ──────────────────────────────────────────────────────────

_INSTALL_DIR = Path("/opt/slop")
_DATA_DIR = Path("/var/lib/slop")


def _make_state(**overrides) -> StateFile:
    defaults = {
        "schema_version": 1,
        "slop_version": "5.0.0",
        "phase": "installed",
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z",
        "install_dir": str(_INSTALL_DIR),
        "data_dir": str(_DATA_DIR),
        "install_user": "slop",
        "distro": "ubuntu",
        "distro_version": "24.04",
        "port": 8080,
        "smoke_test_passed": True,
    }
    defaults.update(overrides)
    return StateFile(**defaults)


def _proc(returncode: int = 0, stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


def _args(yes: bool = True, install_dir: str = str(_INSTALL_DIR)) -> SimpleNamespace:
    return SimpleNamespace(yes=yes, install_dir=install_dir)


# ── Uninstall/purge run helpers ───────────────────────────────────────────────

_UNINSTALL_SUCCESS_SEQ = [
    _proc(0),  # systemctl stop
    _proc(0),  # systemctl disable
    _proc(0),  # rm -f unit file
    _proc(0),  # daemon-reload
    _proc(0),  # rm -rf install_dir
    _proc(1),  # getent passwd → user absent; userdel skipped
    _proc(1),  # getent group → group absent; groupdel skipped
]

_PURGE_SUCCESS_SEQ = [
    *_UNINSTALL_SUCCESS_SEQ,
    _proc(0),  # rm -rf data_dir
    _proc(0, ""),  # docker ps (no containers)
    _proc(0, ""),  # docker volume ls (no volumes)
]


def _run_uninstall(
    state=None,
    yes: bool = True,
    is_tty: bool = False,
    stdin_response: str = "",
    run_seq=None,
    install_dir: str = str(_INSTALL_DIR),
):
    if state is None:
        state = _make_state()
    errors: list = []
    printed: list = []

    def _state_read(_path):
        if isinstance(state, Exception):
            raise state
        return state

    seq = list(run_seq if run_seq is not None else _UNINSTALL_SUCCESS_SEQ)
    seq_iter = iter(seq)

    rc = run_uninstall(
        _args(yes=yes, install_dir=install_dir),
        state_read=_state_read,
        stdin_is_tty=lambda: is_tty,
        stdin_readline=lambda: stdin_response + "\n",
        run=lambda _cmd: next(seq_iter),
        print_fn=printed.append,
        err_fn=errors.append,
    )
    return rc, errors, printed


def _run_purge(
    state=None,
    yes: bool = True,
    is_tty: bool = False,
    stdin_response: str = "",
    run_seq=None,
    install_dir: str = str(_INSTALL_DIR),
):
    if state is None:
        state = _make_state()
    errors: list = []
    printed: list = []

    def _state_read(_path):
        if isinstance(state, Exception):
            raise state
        return state

    seq = list(run_seq if run_seq is not None else _PURGE_SUCCESS_SEQ)
    seq_iter = iter(seq)

    rc = run_purge(
        _args(yes=yes, install_dir=install_dir),
        state_read=_state_read,
        stdin_is_tty=lambda: is_tty,
        stdin_readline=lambda: stdin_response + "\n",
        resolve_hostname=lambda: "myhost",
        run=lambda _cmd: next(seq_iter),
        print_fn=printed.append,
        err_fn=errors.append,
    )
    return rc, errors, printed


def _run_clean(
    state=None,
    yes: bool = True,
    is_tty: bool = False,
    stdin_response: str = "",
    run_seq=None,
    api_results=None,
    install_dir: str = str(_INSTALL_DIR),
):
    if state is None:
        state = _make_state()
    errors: list = []
    printed: list = []

    def _state_read(_path):
        if isinstance(state, Exception):
            raise state
        return state

    seq = list(run_seq if run_seq is not None else [])
    seq_iter = iter(seq)

    api_iter = iter(api_results if api_results is not None else [])

    rc = run_clean(
        _args(yes=yes, install_dir=install_dir),
        state_read=_state_read,
        stdin_is_tty=lambda: is_tty,
        stdin_readline=lambda: stdin_response + "\n",
        resolve_hostname=lambda: "myhost",
        run=lambda _cmd: next(seq_iter),
        api_remove=lambda key, port: next(api_iter),
        print_fn=printed.append,
        err_fn=errors.append,
    )
    return rc, errors, printed


# ── §A.2 / §A.3 / §A.3.5 — _read_state refusals ─────────────────────────────


class TestReadState:
    def test_none_return_emits_no_install_detected_message(self):
        errors = []
        result = _read_state(
            _INSTALL_DIR,
            state_read=lambda _p: None,
            err_fn=errors.append,
        )
        assert result is None
        assert any("No v5 slop install detected" in e for e in errors)

    def test_none_return_message_contains_install_dir(self):
        errors = []
        _read_state(_INSTALL_DIR, state_read=lambda _p: None, err_fn=errors.append)
        assert any(str(_INSTALL_DIR) in e for e in errors)

    def test_corrupted_error_forwarded_verbatim(self):
        errors = []
        exc = StateFileCorruptedError("json parse fail at line 7")

        def _raise(_p):
            raise exc

        result = _read_state(_INSTALL_DIR, state_read=_raise, err_fn=errors.append)
        assert result is None
        assert any("json parse fail at line 7" in e for e in errors)

    def test_newer_schema_error_forwarded_verbatim(self):
        errors = []
        exc = StateFileNewerSchemaError("schema version 9 > supported 1")

        def _raise(_p):
            raise exc

        result = _read_state(_INSTALL_DIR, state_read=_raise, err_fn=errors.append)
        assert result is None
        assert any("schema version 9" in e for e in errors)

    def test_permission_error_emits_try_sudo(self):
        errors = []

        def _raise(_p):
            raise PermissionError("Permission denied")

        _read_state(_INSTALL_DIR, state_read=_raise, err_fn=errors.append)
        joined = " ".join(errors)
        assert "sudo" in joined.lower() or "Permission denied" in joined

    def test_permission_error_returns_none(self):
        def _raise(_p):
            raise PermissionError("Permission denied")

        result = _read_state(_INSTALL_DIR, state_read=_raise, err_fn=lambda _: None)
        assert result is None

    def test_successful_read_returns_state_file(self):
        sf = _make_state()
        result = _read_state(_INSTALL_DIR, state_read=lambda _p: sf, err_fn=lambda _: None)
        assert result is sf


# ── §A.4 — _check_pipe_refusal ───────────────────────────────────────────────


class TestPipeRefusal:
    def test_pipe_without_yes_returns_true(self):
        assert _check_pipe_refusal(is_tty=False, yes=False, err_fn=lambda _: None) is True

    def test_pipe_without_yes_emits_pass_yes_message(self):
        errors = []
        _check_pipe_refusal(is_tty=False, yes=False, err_fn=errors.append)
        assert any("--yes" in e for e in errors)

    def test_pipe_with_yes_returns_false(self):
        assert _check_pipe_refusal(is_tty=False, yes=True, err_fn=lambda _: None) is False

    def test_tty_without_yes_returns_false(self):
        assert _check_pipe_refusal(is_tty=True, yes=False, err_fn=lambda _: None) is False

    def test_tty_with_yes_returns_false(self):
        assert _check_pipe_refusal(is_tty=True, yes=True, err_fn=lambda _: None) is False


# ── §A.4 — _prompt_confirm ───────────────────────────────────────────────────


class TestPromptConfirm:
    def test_yes_flag_returns_true_without_reading_stdin(self):
        def _bad_readline():
            raise AssertionError("stdin should not be read when --yes is set")

        result = _prompt_confirm(
            "prompt", yes=True, is_tty=True, stdin_readline=_bad_readline, print_fn=lambda _: None
        )
        assert result is True

    def test_y_answer_returns_true(self):
        result = _prompt_confirm(
            "prompt", yes=False, is_tty=True, stdin_readline=lambda: "y", print_fn=lambda _: None
        )
        assert result is True

    def test_yes_answer_returns_true(self):
        result = _prompt_confirm(
            "prompt", yes=False, is_tty=True, stdin_readline=lambda: "yes", print_fn=lambda _: None
        )
        assert result is True

    def test_uppercase_y_returns_true(self):
        result = _prompt_confirm(
            "prompt", yes=False, is_tty=True, stdin_readline=lambda: "Y\n", print_fn=lambda _: None
        )
        assert result is True

    def test_empty_answer_returns_false(self):
        result = _prompt_confirm(
            "prompt", yes=False, is_tty=True, stdin_readline=lambda: "", print_fn=lambda _: None
        )
        assert result is False

    def test_n_answer_returns_false(self):
        result = _prompt_confirm(
            "prompt", yes=False, is_tty=True, stdin_readline=lambda: "n", print_fn=lambda _: None
        )
        assert result is False

    def test_prompt_text_is_printed(self):
        printed = []
        _prompt_confirm(
            "Proceed? [y/N]:",
            yes=False,
            is_tty=True,
            stdin_readline=lambda: "y",
            print_fn=printed.append,
        )
        assert any("Proceed?" in p for p in printed)


# ── §A.2/§A.3/§A.3.5/§A.5 — refusals via run_uninstall entry point ──────────


class TestRunUninstallRefusals:
    def test_a2_no_state_file_exits_1(self):
        errors = []

        def _state_read(_path):
            return None

        rc = run_uninstall(
            _args(yes=True),
            state_read=_state_read,
            stdin_is_tty=lambda: False,
            stdin_readline=lambda: "",
            run=lambda _cmd: _proc(0),
            print_fn=lambda _: None,
            err_fn=errors.append,
        )
        assert rc == 1
        assert any("No v5 slop install detected" in e for e in errors)

    def test_a3_corrupted_state_exits_1(self):
        exc = StateFileCorruptedError("invalid json")
        rc, errors, _ = _run_uninstall(state=exc, run_seq=[])
        assert rc == 1
        assert any("invalid json" in e for e in errors)

    def test_a35_permission_error_exits_1(self):
        rc, errors, _ = _run_uninstall(state=PermissionError("denied"), run_seq=[])
        assert rc == 1
        assert any("sudo" in e.lower() or "Permission denied" in e for e in errors)

    def test_a35_permission_error_before_side_effects(self):
        run_called = []
        errors = []

        def _state_read(_p):
            raise PermissionError("denied")

        run_uninstall(
            _args(yes=True),
            state_read=_state_read,
            stdin_is_tty=lambda: False,
            stdin_readline=lambda: "",
            run=lambda _cmd: run_called.append(_cmd) or _proc(0),
            print_fn=lambda _: None,
            err_fn=errors.append,
        )
        assert run_called == [], "run() must not be called before state-read error"

    def test_a4_pipe_without_yes_exits_1(self):
        rc, errors, _ = _run_uninstall(yes=False, is_tty=False, run_seq=[])
        assert rc == 1
        assert any("--yes" in e for e in errors)

    def test_a5_yes_does_not_override_no_state_file(self):
        errors = []

        def _state_read(_p):
            return None

        rc = run_uninstall(
            _args(yes=True),
            state_read=_state_read,
            stdin_is_tty=lambda: False,
            stdin_readline=lambda: "",
            run=lambda _cmd: _proc(0),
            print_fn=lambda _: None,
            err_fn=errors.append,
        )
        assert rc == 1

    def test_a5_yes_does_not_override_corrupted_state(self):
        rc, _errors, _ = _run_uninstall(state=StateFileCorruptedError("corrupt"), run_seq=[])
        assert rc == 1

    def test_a5_yes_does_not_override_permission_error(self):
        rc, _errors, _ = _run_uninstall(state=PermissionError("denied"), run_seq=[])
        assert rc == 1


# ── §A.4/§A.5 — confirmation UX via run_uninstall ────────────────────────────


class TestRunUninstallConfirmation:
    def test_cancel_exits_0_without_pipeline(self):
        run_called = []
        errors = []

        def _state_read(_p):
            return _make_state()

        rc = run_uninstall(
            _args(yes=False),
            state_read=_state_read,
            stdin_is_tty=lambda: True,
            stdin_readline=lambda: "n",
            run=lambda _cmd: run_called.append(_cmd) or _proc(0),
            print_fn=lambda _: None,
            err_fn=errors.append,
        )
        assert rc == 0
        assert run_called == [], "pipeline must not run when operator cancels"

    def test_cancel_prints_cancelled_message(self):
        printed = []

        def _state_read(_p):
            return _make_state()

        run_uninstall(
            _args(yes=False),
            state_read=_state_read,
            stdin_is_tty=lambda: True,
            stdin_readline=lambda: "n",
            run=lambda _cmd: _proc(0),
            print_fn=printed.append,
            err_fn=lambda _: None,
        )
        assert any("cancelled" in p.lower() for p in printed)

    def test_yes_flag_skips_prompt(self):
        readline_called = []

        def _state_read(_p):
            return _make_state()

        seq_iter = iter(_UNINSTALL_SUCCESS_SEQ)
        run_uninstall(
            _args(yes=True),
            state_read=_state_read,
            stdin_is_tty=lambda: True,
            stdin_readline=lambda: readline_called.append(1) or "n",
            run=lambda _cmd: next(seq_iter),
            print_fn=lambda _: None,
            err_fn=lambda _: None,
        )
        assert readline_called == [], "stdin must not be read when --yes is set"

    def test_pipe_with_yes_proceeds(self):
        rc, _, _ = _run_uninstall(yes=True, is_tty=False)
        assert rc == 0


# ── §B pipeline — failure modes ──────────────────────────────────────────────


class TestRunUninstallPipeline:
    def test_u1_failure_stops_pipeline_exits_1(self):
        seq = [
            _proc(1),  # systemctl stop → fails
            _proc(0),  # test -e unit → present (rc=0) → hard fail
        ]
        rc, errors, _ = _run_uninstall(run_seq=seq)
        assert rc == 1
        assert any("did not stop" in e or "service" in e.lower() for e in errors)

    def test_u2_failure_stops_pipeline_exits_1(self):
        seq = [
            _proc(0),  # stop
            _proc(0),  # disable
            _proc(1),  # rm unit → fails
        ]
        rc, errors, _ = _run_uninstall(run_seq=seq)
        assert rc == 1
        assert any("unit file" in e.lower() or "could not be removed" in e for e in errors)

    def test_u3_failure_stops_pipeline_exits_1(self):
        seq = [
            _proc(0),  # stop
            _proc(0),  # disable
            _proc(0),  # rm unit
            _proc(0),  # daemon-reload
            _proc(1),  # rm install_dir → fails
        ]
        rc, errors, _ = _run_uninstall(run_seq=seq)
        assert rc == 1
        assert any(
            "install directory" in e.lower() or "could not be fully removed" in e for e in errors
        )

    def test_u4_failure_is_late_exits_1(self):
        user_entry = "slop:x:50:50::/nonexistent:/usr/sbin/nologin"
        seq = [
            _proc(0),  # stop
            _proc(0),  # disable
            _proc(0),  # rm unit
            _proc(0),  # daemon-reload
            _proc(0),  # rm install_dir
            _proc(0, user_entry),  # getent passwd → present, correct attrs
            _proc(1),  # userdel → fails (late failure U4)
            _proc(1),  # getent group → absent; groupdel skipped
        ]
        rc, errors, _ = _run_uninstall(run_seq=seq)
        assert rc == 1
        assert any("user" in e.lower() or "userdel" in e.lower() for e in errors)

    def test_u4b_failure_is_late_exits_1(self):
        seq = [
            _proc(0),  # stop
            _proc(0),  # disable
            _proc(0),  # rm unit
            _proc(0),  # daemon-reload
            _proc(0),  # rm install_dir
            _proc(1),  # getent passwd → user absent; userdel skipped
            _proc(0, "slop:x:999:slop"),  # getent group → present, no extra
            _proc(1),  # groupdel → fails (late failure U4b)
        ]
        rc, errors, _ = _run_uninstall(run_seq=seq)
        assert rc == 1
        assert any("group" in e.lower() or "groupdel" in e.lower() for e in errors)

    def test_a6_user_mismatch_skips_userdel_continues(self):
        # uid=1001 → mismatch → userdel skipped; pipeline continues
        user_entry = "slop:x:1001:1001::/home/slop:/bin/bash"
        seq = [
            _proc(0),  # stop
            _proc(0),  # disable
            _proc(0),  # rm unit
            _proc(0),  # daemon-reload
            _proc(0),  # rm install_dir
            _proc(0, user_entry),  # getent passwd → mismatch → userdel SKIPPED
            _proc(1),  # getent group → absent; groupdel skipped
        ]
        rc, errors, _ = _run_uninstall(run_seq=seq)
        assert rc == 0
        assert any("unexpected attributes" in e or "will not remove" in e for e in errors)

    def test_a65_group_mismatch_skips_groupdel_continues(self):
        # extra member "thirdparty" → mismatch → groupdel skipped
        group_entry = "slop:x:999:thirdparty"
        seq = [
            _proc(0),  # stop
            _proc(0),  # disable
            _proc(0),  # rm unit
            _proc(0),  # daemon-reload
            _proc(0),  # rm install_dir
            _proc(1),  # getent passwd → user absent; userdel skipped
            _proc(0, group_entry),  # getent group → extra members → groupdel SKIPPED
        ]
        rc, errors, _ = _run_uninstall(run_seq=seq)
        assert rc == 0
        assert any("members not added" in e or "will not remove" in e for e in errors)

    def test_group_absent_after_userdel_skips_groupdel_exits_0(self):
        # Ubuntu: user present → userdel runs → removes primary group as side effect;
        # getent group rc=1 → groupdel skipped → exit 0.
        user_entry = "slop:x:50:50::/nonexistent:/usr/sbin/nologin"
        seq = [
            _proc(0),
            _proc(0),
            _proc(0),
            _proc(0),
            _proc(0),
            _proc(0, user_entry),  # getent passwd → present, correct attrs
            _proc(0),  # userdel → success (also removes primary group on Ubuntu)
            _proc(1),  # getent group → absent (userdel removed it)
        ]
        rc, errors, _ = _run_uninstall(run_seq=seq)
        assert rc == 0
        assert not any("group" in e.lower() for e in errors)

    def test_group_present_no_extra_groupdel_exits_0(self):
        # Non-Ubuntu: user present → userdel runs but does NOT remove primary group;
        # getent group rc=0, no extra members → groupdel called → exit 0.
        user_entry = "slop:x:50:50::/nonexistent:/usr/sbin/nologin"
        group_entry = "slop:x:999:slop"
        seq = [
            _proc(0),
            _proc(0),
            _proc(0),
            _proc(0),
            _proc(0),
            _proc(0, user_entry),  # getent passwd → present, correct attrs
            _proc(0),  # userdel → success (group NOT removed)
            _proc(0, group_entry),  # getent group → present, only install_user
            _proc(0),  # groupdel → success
        ]
        rc, errors, _ = _run_uninstall(run_seq=seq)
        assert rc == 0
        assert not errors

    def test_successful_uninstall_exits_0(self):
        rc, _, _ = _run_uninstall()
        assert rc == 0

    def test_successful_uninstall_prints_complete(self):
        _, _, printed = _run_uninstall()
        assert any("Uninstall complete" in p for p in printed)


# ── §B — class predictions ───────────────────────────────────────────────────


class TestClassPredictions:
    def test_b1_phase_installing_proceeds(self):
        """B.1: uninstall with phase=installing (interrupted install) proceeds normally."""
        state = _make_state(phase="installing", completed_at=None)
        rc, _, _ = _run_uninstall(state=state)
        assert rc == 0

    def test_b2_smoke_failed_proceeds(self):
        """B.2: uninstall with smoke_test_passed=False proceeds normally."""
        state = _make_state(smoke_test_passed=False)
        rc, _, _ = _run_uninstall(state=state)
        assert rc == 0

    def test_b3_no_state_file_refusal_a2(self):
        """B.3: no state file but install dir exists → §A.2 refusal."""
        errors = []

        def _state_read(_p):
            return None

        rc = run_uninstall(
            _args(yes=True),
            state_read=_state_read,
            stdin_is_tty=lambda: False,
            stdin_readline=lambda: "",
            run=lambda _cmd: _proc(0),
            print_fn=lambda _: None,
            err_fn=errors.append,
        )
        assert rc == 1
        assert any("No v5 slop install detected" in e for e in errors)

    def test_b4_corrupted_state_refusal_a3(self):
        """B.4: corrupted state file → §A.3 refusal."""
        rc, _errors, _ = _run_uninstall(state=StateFileCorruptedError("bad json"))
        assert rc == 1

    def test_a1_userdel_blocked_by_running_process(self):
        """A.1: userdel fails because process still running → U4 late failure."""
        user_entry = "slop:x:50:50::/nonexistent:/usr/sbin/nologin"
        # user present, correct attrs → userdel called → userdel fails (process running)
        seq = [
            _proc(0),
            _proc(0),
            _proc(0),
            _proc(0),
            _proc(0),
            _proc(0, user_entry),  # getent passwd → present, correct attrs
            _proc(1, "user slop is currently used by process 999"),  # userdel blocked
            _proc(1),  # getent group → absent; groupdel skipped
        ]
        rc, _errors, _ = _run_uninstall(run_seq=seq)
        assert rc == 1

    def test_a3_state_file_permission_denied_try_sudo(self):
        """A.3: state file exists but PermissionError → §A.3.5 try-sudo message."""
        rc, errors, _ = _run_uninstall(state=PermissionError("Permission denied"))
        assert rc == 1
        joined = " ".join(errors)
        assert "sudo" in joined.lower() or "permission" in joined.lower()

    def test_a4_already_stopped_service_idempotent(self):
        """A.4: systemctl stop on already-stopped unit exits 0 idempotently."""
        rc, _, _ = _run_uninstall()  # default seq has systemctl stop → rc=0
        assert rc == 0

    def test_unit_absent_systemctl_stop_soft_fail_exits_0(self):
        # Unit file already absent (e.g., purge after prior uninstall);
        # systemctl stop returns non-zero → unit-absent check passes → soft-fail → pipeline continues.
        seq = [
            _proc(1),  # systemctl stop → fails (unit absent)
            _proc(1),  # test -e unit → absent (rc=1) → soft-fail
            _proc(0),  # systemctl disable
            _proc(0),  # rm -f unit file
            _proc(0),  # daemon-reload
            _proc(0),  # rm -rf install_dir
            _proc(1),  # getent passwd → user absent; userdel skipped
            _proc(1),  # getent group → absent; groupdel skipped
        ]
        rc, errors, _ = _run_uninstall(run_seq=seq)
        assert rc == 0
        assert not any("did not stop" in e for e in errors)

    def test_unit_present_systemctl_stop_hard_fail_exits_1(self):
        # Unit file present but systemctl stop fails → genuine failure → hard-fail → exit 1.
        seq = [
            _proc(1),  # systemctl stop → fails
            _proc(0),  # test -e unit → present (rc=0) → hard fail
        ]
        rc, errors, _ = _run_uninstall(run_seq=seq)
        assert rc == 1
        assert any("did not stop" in e for e in errors)

    def test_user_absent_skips_userdel_exits_0(self):
        # User already absent before step 7 (e.g., removed by prior uninstall);
        # userdel is skipped entirely → exit 0.
        seq = [
            _proc(0),
            _proc(0),
            _proc(0),
            _proc(0),
            _proc(0),
            _proc(1),  # getent passwd → absent; userdel skipped
            _proc(1),  # getent group → absent; groupdel skipped
        ]
        rc, errors, _ = _run_uninstall(run_seq=seq)
        assert rc == 0
        assert not any("user" in e.lower() for e in errors)

    def test_user_present_no_mismatch_userdel_exits_0(self):
        # User present with correct attrs → userdel called → exits 0.
        user_entry = "slop:x:50:50::/nonexistent:/usr/sbin/nologin"
        seq = [
            _proc(0),
            _proc(0),
            _proc(0),
            _proc(0),
            _proc(0),
            _proc(0, user_entry),  # getent passwd → present, correct attrs
            _proc(0),  # userdel → success
            _proc(1),  # getent group → absent; groupdel skipped
        ]
        rc, errors, _ = _run_uninstall(run_seq=seq)
        assert rc == 0
        assert not errors

    def test_a5_docker_daemon_down_during_purge(self):
        """A.5: docker daemon down during purge → U6 failure."""
        seq = [
            *_UNINSTALL_SUCCESS_SEQ,
            _proc(0),  # rm -rf data_dir
            _proc(1),  # docker ps fails (daemon down) → U6 late failure
            _proc(1),  # docker volume ls fails → U7 late failure
        ]
        rc, errors, _ = _run_purge(run_seq=seq)
        assert rc == 1
        assert any("Docker daemon" in e or "docker" in e.lower() for e in errors)

    def test_c4_clean_service_not_active_exits_1(self):
        """C.4: clean requires service active."""
        seq = [
            _proc(1, "inactive"),  # systemctl is-active → not active
        ]
        rc, errors, _ = _run_clean(run_seq=seq)
        assert rc == 1
        assert any("not running" in e or "service" in e.lower() for e in errors)

    def test_c2_clean_docker_unreachable_exits_1(self):
        """C.2: docker daemon unreachable during clean → exit 1."""
        seq = [
            _proc(0, "active"),  # is-active → active
            _proc(1),  # docker ps fails (daemon down)
        ]
        rc, errors, _ = _run_clean(run_seq=seq)
        assert rc == 1
        assert any("Docker" in e or "docker" in e.lower() for e in errors)

    def test_s1_toctou_guard_data_dir_mismatch(self):
        """S.1: TOCTOU guard fires when data_dir resolves differently than state-file path."""
        sf = _make_state(data_dir="/var/lib/slop")
        errors = []
        printed = []

        def _noop_run(_cmd):
            return _proc(0)

        # Pipeline step 9: real_data (different_path.resolve()) != state_data
        rc = _run_removal_pipeline(
            "purge",
            _INSTALL_DIR,
            Path("/different/resolved/path"),  # mismatches sf.data_dir
            sf,
            run=_noop_run,
            print_fn=printed.append,
            err_fn=errors.append,
        )
        assert rc == 1
        assert any(
            "mismatch" in e.lower() or "symlink" in e.lower() or "Refusing" in e for e in errors
        )

    def test_s6_group_extra_members_skips_groupdel(self):
        """S.6: group with extra members → §A.6.5 refuses groupdel; rest continues."""
        group_entry = "slop:x:999:thirdparty,otheruser"
        seq = [
            _proc(0),
            _proc(0),
            _proc(0),
            _proc(0),
            _proc(0),
            _proc(1),  # getent passwd → user absent; userdel skipped
            _proc(0, group_entry),  # getent group → extra members
        ]
        rc, errors, _ = _run_uninstall(run_seq=seq)
        assert rc == 0
        assert any(
            "members not added" in e or "additional members" in e or "will not remove" in e
            for e in errors
        )


# ── run_purge ─────────────────────────────────────────────────────────────────


class TestRunPurge:
    def test_no_state_file_exits_1(self):
        errors = []

        def _state_read(_p):
            return None

        rc = run_purge(
            _args(yes=True),
            state_read=_state_read,
            stdin_is_tty=lambda: False,
            stdin_readline=lambda: "",
            resolve_hostname=lambda: "h",
            run=lambda _cmd: _proc(0),
            print_fn=lambda _: None,
            err_fn=errors.append,
        )
        assert rc == 1

    def test_pipe_without_yes_exits_1(self):
        rc, errors, _ = _run_purge(yes=False, is_tty=False, run_seq=[])
        assert rc == 1
        assert any("--yes" in e for e in errors)

    def test_cancel_exits_0(self):
        printed = []

        def _state_read(_p):
            return _make_state()

        rc = run_purge(
            _args(yes=False),
            state_read=_state_read,
            stdin_is_tty=lambda: True,
            stdin_readline=lambda: "n",
            resolve_hostname=lambda: "h",
            run=lambda _cmd: _proc(0),
            print_fn=printed.append,
            err_fn=lambda _: None,
        )
        assert rc == 0
        assert any("cancelled" in p.lower() for p in printed)

    def test_successful_purge_exits_0(self):
        rc, _, _ = _run_purge()
        assert rc == 0

    def test_successful_purge_prints_complete(self):
        _, _, printed = _run_purge()
        assert any("Purge complete" in p for p in printed)

    def test_docker_daemon_unreachable_u6_late_failure(self):
        seq = [
            *_UNINSTALL_SUCCESS_SEQ,
            _proc(0),  # rm -rf data_dir
            _proc(1),  # docker ps fails → U6
            _proc(0),  # docker volume ls (continues after U6)
        ]
        rc, errors, _ = _run_purge(run_seq=seq)
        assert rc == 1
        assert any("Docker daemon" in e or "daemon" in e.lower() for e in errors)


# ── run_clean ─────────────────────────────────────────────────────────────────


class TestRunClean:
    def _clean_active_no_apps(self):
        return [
            _proc(0, "active"),  # systemctl is-active
            _proc(0, ""),  # docker ps (no apps)
        ]

    def _clean_active_one_app(self, app_key="myapp"):
        return [
            _proc(0, "active"),
            _proc(0, f"{app_key}-container\t{app_key}"),
        ]

    def test_no_state_file_exits_1(self):
        errors = []

        def _state_read(_p):
            return None

        rc = run_clean(
            _args(yes=True),
            state_read=_state_read,
            stdin_is_tty=lambda: False,
            stdin_readline=lambda: "",
            resolve_hostname=lambda: "h",
            run=lambda _cmd: _proc(0),
            api_remove=lambda k, p: {},
            print_fn=lambda _: None,
            err_fn=errors.append,
        )
        assert rc == 1

    def test_service_not_active_exits_1(self):
        seq = [_proc(1, "inactive")]
        rc, errors, _ = _run_clean(run_seq=seq)
        assert rc == 1
        assert any("not running" in e or "service" in e.lower() for e in errors)

    def test_docker_daemon_unreachable_exits_1(self):
        seq = [_proc(0, "active"), _proc(1)]
        rc, errors, _ = _run_clean(run_seq=seq)
        assert rc == 1
        assert any("Docker" in e or "docker" in e.lower() for e in errors)

    def test_no_managed_apps_exits_0(self):
        rc, _, _ = _run_clean(run_seq=self._clean_active_no_apps())
        assert rc == 0

    def test_no_managed_apps_prints_nothing_to_clean(self):
        _, _, printed = _run_clean(run_seq=self._clean_active_no_apps())
        assert any("Nothing to clean" in p for p in printed)

    def test_pipe_without_yes_exits_1(self):
        seq = [_proc(0, "active"), _proc(0, "a-cont\ta-key")]
        rc, errors, _ = _run_clean(yes=False, is_tty=False, run_seq=seq)
        assert rc == 1
        assert any("--yes" in e for e in errors)

    def test_cancel_exits_0(self):
        seq = [_proc(0, "active"), _proc(0, "a-cont\ta-key")]
        printed = []
        errors = []

        def _state_read(_p):
            return _make_state()

        run_clean(
            _args(yes=False),
            state_read=_state_read,
            stdin_is_tty=lambda: True,
            stdin_readline=lambda: "n",
            resolve_hostname=lambda: "h",
            run=lambda _cmd: iter(seq).__next__() if False else next(iter(seq)),
            api_remove=lambda k, p: {},
            print_fn=printed.append,
            err_fn=errors.append,
        )
        # Use fresh iterator
        seq2 = iter([_proc(0, "active"), _proc(0, "a-cont\ta-key")])
        rc2 = run_clean(
            _args(yes=False),
            state_read=lambda _p: _make_state(),
            stdin_is_tty=lambda: True,
            stdin_readline=lambda: "n",
            resolve_hostname=lambda: "h",
            run=lambda _cmd: next(seq2),
            api_remove=lambda k, p: {},
            print_fn=printed.append,
            err_fn=errors.append,
        )
        assert rc2 == 0
        assert any("cancelled" in p.lower() for p in printed)

    def test_c5_all_apps_ok_exits_0(self):
        seq = [_proc(0, "active"), _proc(0, "myapp-cont\tmyapp")]
        api_results = [{"ok": True, "steps": [], "error": ""}]
        rc, _, _ = _run_clean(run_seq=seq, api_results=api_results)
        assert rc == 0

    def test_c5_any_failed_exits_1(self):
        seq = [_proc(0, "active"), _proc(0, "myapp-cont\tmyapp")]
        api_results = [{"ok": False, "steps": [], "error": "container removal failed"}]
        rc, _, _ = _run_clean(run_seq=seq, api_results=api_results)
        assert rc == 1

    def test_c5_warning_only_exits_0(self):
        seq = [_proc(0, "active"), _proc(0, "myapp-cont\tmyapp")]
        api_results = [
            {
                "ok": True,
                "steps": [{"status": "warning", "name": "hostname", "message": "skipped"}],
                "error": "",
            }
        ]
        rc, _, _ = _run_clean(run_seq=seq, api_results=api_results)
        assert rc == 0

    def test_c8_url_shown_on_success(self):
        seq = [_proc(0, "active"), _proc(0, "myapp-cont\tmyapp")]
        api_results = [{"ok": True, "steps": [], "error": ""}]
        _, _, printed = _run_clean(run_seq=seq, api_results=api_results)
        assert any("http://" in p or "SLOP remains running" in p for p in printed)

    def test_c8_url_suppressed_on_failure(self):
        seq = [_proc(0, "active"), _proc(0, "myapp-cont\tmyapp")]
        api_results = [{"ok": False, "steps": [], "error": "removal failed"}]
        _, _, printed = _run_clean(run_seq=seq, api_results=api_results)
        url_lines = [p for p in printed if "http://" in p and "SLOP remains running" in p]
        assert url_lines == []

    def test_a5_yes_does_not_override_no_state_file_in_clean(self):
        errors = []

        def _state_read(_p):
            return None

        rc = run_clean(
            _args(yes=True),
            state_read=_state_read,
            stdin_is_tty=lambda: False,
            stdin_readline=lambda: "",
            resolve_hostname=lambda: "h",
            run=lambda _cmd: _proc(0),
            api_remove=lambda k, p: {},
            print_fn=lambda _: None,
            err_fn=errors.append,
        )
        assert rc == 1


# ── §C.6 / INV-16 — _print_clean_output ──────────────────────────────────────


class TestPrintCleanOutput:
    def test_ok_row_format(self):
        printed = []
        _print_clean_output(
            [("jellyfin", {"ok": True, "steps": [], "error": ""})],
            orphans=[],
            print_fn=printed.append,
        )
        rows = [p for p in printed if "jellyfin" in p]
        assert rows
        assert "ok" in rows[0]
        assert "(stopped, unwired, removed)" in rows[0]

    def test_warning_row_format(self):
        printed = []
        _print_clean_output(
            [
                (
                    "sonarr",
                    {
                        "ok": True,
                        "steps": [{"status": "warning", "name": "hostname", "message": "x"}],
                        "error": "",
                    },
                )
            ],
            orphans=[],
            print_fn=printed.append,
        )
        rows = [p for p in printed if "sonarr" in p]
        assert rows
        assert "warning" in rows[0]

    def test_failed_row_format(self):
        printed = []
        _print_clean_output(
            [("radarr", {"ok": False, "steps": [], "error": "container removal failed"})],
            orphans=[],
            print_fn=printed.append,
        )
        rows = [p for p in printed if "radarr" in p]
        assert rows
        assert "failed" in rows[0]
        assert "container removal failed" in rows[0]

    def test_failed_row_with_step_detail(self):
        printed = []
        _print_clean_output(
            [
                (
                    "radarr",
                    {
                        "ok": False,
                        "steps": [{"status": "error", "name": "stop", "message": "timeout"}],
                        "error": "",
                    },
                )
            ],
            orphans=[],
            print_fn=printed.append,
        )
        rows = [p for p in printed if "radarr" in p]
        assert "stop failed: timeout" in rows[0]

    def test_summary_line_all_ok(self):
        printed = []
        _print_clean_output(
            [
                ("a", {"ok": True, "steps": [], "error": ""}),
                ("b", {"ok": True, "steps": [], "error": ""}),
            ],
            orphans=[],
            print_fn=printed.append,
        )
        summary = [p for p in printed if "Summary" in p]
        assert summary
        assert "2 ok" in summary[0]

    def test_summary_line_mixed(self):
        printed = []
        _print_clean_output(
            [
                ("a", {"ok": True, "steps": [], "error": ""}),
                ("b", {"ok": False, "steps": [], "error": "err"}),
            ],
            orphans=["orphan-1"],
            print_fn=printed.append,
        )
        summary = [p for p in printed if "Summary" in p]
        assert summary
        assert "1 ok" in summary[0]
        assert "1 failed" in summary[0]
        assert "orphan" in summary[0]

    def test_orphans_reported(self):
        printed = []
        _print_clean_output(
            results=[],
            orphans=["legacy-container-7"],
            print_fn=printed.append,
        )
        assert any("legacy-container-7" in p for p in printed)

    def test_all_ok_exits_0(self):
        rc = _print_clean_output(
            [("a", {"ok": True, "steps": [], "error": ""})],
            orphans=[],
            print_fn=lambda _: None,
        )
        assert rc == 0

    def test_any_failed_exits_1(self):
        rc = _print_clean_output(
            [("a", {"ok": False, "steps": [], "error": "err"})],
            orphans=[],
            print_fn=lambda _: None,
        )
        assert rc == 1

    def test_warning_only_exits_0(self):
        rc = _print_clean_output(
            [
                (
                    "a",
                    {
                        "ok": True,
                        "steps": [{"status": "warning", "name": "w", "message": "m"}],
                        "error": "",
                    },
                )
            ],
            orphans=[],
            print_fn=lambda _: None,
        )
        assert rc == 0

    def test_inv16_row_greppable_format(self):
        """INV-16: each row matches <app-key>\\s+(ok|warning|failed)\\s+\\(.*\\)."""
        import re

        printed = []
        _print_clean_output(
            [
                ("jellyfin", {"ok": True, "steps": [], "error": ""}),
                ("sonarr", {"ok": False, "steps": [], "error": "err"}),
            ],
            orphans=[],
            print_fn=printed.append,
        )
        pattern = re.compile(r"\w[\w-]+\s+(ok|warning|failed)\s+\(.*\)")
        app_rows = [p for p in printed if "jellyfin" in p or "sonarr" in p]
        for row in app_rows:
            assert pattern.search(row.strip()), f"INV-16 format violated: {row!r}"


# ── §A.7 — verify_removed: uninstall mode ────────────────────────────────────


class TestVerifyRemovedUninstall:
    def _vr(self, run_seq, *, install_dir_exists=False, data_dir_exists=True, pre_stat=None):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            install_dir = tmp / "install"
            data_dir = tmp / "data"
            if install_dir_exists:
                install_dir.mkdir()
            if data_dir_exists:
                data_dir.mkdir()
            seq_iter = iter(run_seq)
            return verify_removed(
                install_dir,
                data_dir,
                "uninstall",
                run=lambda _cmd: next(seq_iter),
                pre_data_dir_stat=pre_stat,
            )

    def test_u1_inactive_holds(self):
        vr = self._vr([_proc(1, "inactive"), _proc(1), _proc(1)])
        assert vr.predicates["U1"] is True

    def test_u1_active_fails(self):
        vr = self._vr([_proc(0, "active"), _proc(1), _proc(1)])
        assert vr.predicates["U1"] is False

    def test_u1_unknown_holds(self):
        vr = self._vr([_proc(1, "unknown"), _proc(1), _proc(1)])
        assert vr.predicates["U1"] is True

    def test_u2_unit_absent_holds(self):
        with tempfile.TemporaryDirectory() as td:
            fake_unit = str(Path(td) / "slop.service")  # does not exist
            with patch("installer.uninstall._SYSTEMD_UNIT", fake_unit):
                vr = self._vr([_proc(1, "inactive"), _proc(1), _proc(1)])
        assert vr.predicates["U2"] is True

    def test_u2_unit_present_fails(self):
        with tempfile.TemporaryDirectory() as td:
            fake_unit = Path(td) / "slop.service"
            fake_unit.touch()  # file exists → U2 should fail
            with patch("installer.uninstall._SYSTEMD_UNIT", str(fake_unit)):
                vr = self._vr([_proc(1, "inactive"), _proc(1), _proc(1)])
        assert vr.predicates["U2"] is False

    def test_u3_install_dir_absent_holds(self):
        vr = self._vr([_proc(1, "inactive"), _proc(1), _proc(1)], install_dir_exists=False)
        assert vr.predicates["U3"] is True

    def test_u3_install_dir_present_fails(self):
        vr = self._vr([_proc(1, "inactive"), _proc(1), _proc(1)], install_dir_exists=True)
        assert vr.predicates["U3"] is False

    def test_u4_user_absent_holds(self):
        vr = self._vr([_proc(1, "inactive"), _proc(1), _proc(1)])
        assert vr.predicates["U4"] is True

    def test_u4_user_present_system_attrs_fails(self):
        user_entry = "slop:x:999:999::/nonexistent:/usr/sbin/nologin"
        vr = self._vr([_proc(1, "inactive"), _proc(0, user_entry), _proc(1)])
        assert vr.predicates["U4"] is False

    def test_u4_user_mismatch_skipped_a6(self):
        user_entry = "slop:x:1001:1001::/home/slop:/bin/bash"
        vr = self._vr([_proc(1, "inactive"), _proc(0, user_entry), _proc(1)])
        assert "U4" not in vr.predicates
        assert "U4" in vr.skipped

    def test_u4b_group_absent_holds(self):
        vr = self._vr([_proc(1, "inactive"), _proc(1), _proc(1)])
        assert vr.predicates["U4b"] is True

    def test_u4b_extra_members_skipped_a65(self):
        group_entry = "slop:x:999:slop,thirdparty"
        vr = self._vr([_proc(1, "inactive"), _proc(1), _proc(0, group_entry)])
        assert "U4b" not in vr.predicates
        assert "U4b" in vr.skipped

    def test_u4b_only_slop_member_holds(self):
        group_entry = "slop:x:999:slop"
        vr = self._vr([_proc(1, "inactive"), _proc(1), _proc(0, group_entry)])
        assert vr.predicates["U4b"] is True

    def test_u5a_data_dir_preserved_holds(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            data_dir = tmp / "data"
            data_dir.mkdir()
            pre_stat = data_dir.stat()
            seq_iter = iter([_proc(1, "inactive"), _proc(1), _proc(1)])
            vr = verify_removed(
                tmp / "install",
                data_dir,
                "uninstall",
                run=lambda _cmd: next(seq_iter),
                pre_data_dir_stat=pre_stat,
            )
        assert vr.predicates["U5a"] is True

    def test_u5a_data_dir_gone_fails(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            data_dir = tmp / "data"
            data_dir.mkdir()
            pre_stat = data_dir.stat()
            data_dir.rmdir()
            seq_iter = iter([_proc(1, "inactive"), _proc(1), _proc(1)])
            vr = verify_removed(
                tmp / "install",
                data_dir,
                "uninstall",
                run=lambda _cmd: next(seq_iter),
                pre_data_dir_stat=pre_stat,
            )
        assert vr.predicates["U5a"] is False

    def test_u5a_no_pre_stat_falls_back_to_presence(self):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            data_dir = tmp / "data"
            data_dir.mkdir()
            seq_iter = iter([_proc(1, "inactive"), _proc(1), _proc(1)])
            vr = verify_removed(
                tmp / "install",
                data_dir,
                "uninstall",
                run=lambda _cmd: next(seq_iter),
                pre_data_dir_stat=None,
            )
        assert vr.predicates["U5a"] is True

    def test_uninstall_excludes_u5b_u6_u7(self):
        vr = self._vr([_proc(1, "inactive"), _proc(1), _proc(1)])
        assert "U5b" not in vr.predicates
        assert "U6" not in vr.predicates
        assert "U7" not in vr.predicates

    def test_diagnostics_populated_for_all_uninstall_predicates(self):
        vr = self._vr([_proc(1, "inactive"), _proc(1), _proc(1)])
        for pred in ("U1", "U2", "U3", "U4", "U4b", "U5a"):
            if pred not in vr.skipped:
                assert pred in vr.diagnostics, f"diagnostic missing for {pred}"


# ── §A.7 — verify_removed: purge mode ────────────────────────────────────────


class TestVerifyRemovedPurge:
    def _vr(self, run_seq, *, data_dir_exists=False):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            data_dir = tmp / "data"
            if data_dir_exists:
                data_dir.mkdir()
            seq_iter = iter(run_seq)
            return verify_removed(
                tmp / "install",
                data_dir,
                "purge",
                run=lambda _cmd: next(seq_iter),
            )

    def test_u5b_data_dir_absent_holds(self):
        vr = self._vr(
            [_proc(1, "inactive"), _proc(1), _proc(1), _proc(0, ""), _proc(0, "")],
            data_dir_exists=False,
        )
        assert vr.predicates["U5b"] is True

    def test_u5b_data_dir_present_fails(self):
        vr = self._vr(
            [_proc(1, "inactive"), _proc(1), _proc(1), _proc(0, ""), _proc(0, "")],
            data_dir_exists=True,
        )
        assert vr.predicates["U5b"] is False

    def test_u6_no_containers_holds(self):
        vr = self._vr([_proc(1, "inactive"), _proc(1), _proc(1), _proc(0, ""), _proc(0, "")])
        assert vr.predicates["U6"] is True

    def test_u6_containers_present_fails(self):
        vr = self._vr(
            [
                _proc(1, "inactive"),
                _proc(1),
                _proc(1),
                _proc(0, "container-abc"),
                _proc(0, ""),
            ]
        )
        assert vr.predicates["U6"] is False

    def test_u6_docker_unreachable_fails(self):
        vr = self._vr(
            [
                _proc(1, "inactive"),
                _proc(1),
                _proc(1),
                _proc(1, ""),  # docker ps fails
                _proc(0, ""),
            ]
        )
        assert vr.predicates["U6"] is False

    def test_u7_no_volumes_holds(self):
        vr = self._vr([_proc(1, "inactive"), _proc(1), _proc(1), _proc(0, ""), _proc(0, "")])
        assert vr.predicates["U7"] is True

    def test_u7_volumes_present_fails(self):
        vr = self._vr(
            [
                _proc(1, "inactive"),
                _proc(1),
                _proc(1),
                _proc(0, ""),
                _proc(0, "vol-abc"),
            ]
        )
        assert vr.predicates["U7"] is False

    def test_purge_excludes_u5a(self):
        vr = self._vr([_proc(1, "inactive"), _proc(1), _proc(1), _proc(0, ""), _proc(0, "")])
        assert "U5a" not in vr.predicates


# ── §A.7 / INV-14 — verify_removed: clean mode ───────────────────────────────


class TestVerifyRemovedClean:
    # clean mode calls run() for: U1(is-active), U4(getent passwd),
    # U4b(getent group), U6(docker ps), U7(docker volume ls) — 5 total.
    _BASE: ClassVar = [
        _proc(0, "active"),  # systemctl is-active → U1=False (still running)
        _proc(1),  # getent passwd → U4=True (user absent)
        _proc(1),  # getent group → U4b=True (group absent)
        _proc(0, ""),  # docker ps → U6=True (no containers)
        _proc(0, ""),  # docker volume ls → U7=True (no volumes)
    ]

    def _vr(self, run_seq):
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            seq_iter = iter(run_seq)
            return verify_removed(
                tmp / "install",
                tmp / "data",
                "clean",
                run=lambda _cmd: next(seq_iter),
            )

    def test_u6_no_containers_holds(self):
        vr = self._vr(self._BASE)
        assert vr.predicates["U6"] is True

    def test_u7_no_volumes_holds(self):
        vr = self._vr(self._BASE)
        assert vr.predicates["U7"] is True

    def test_clean_excludes_u5a_u5b(self):
        """U5a (uninstall-only) and U5b (purge-only) are not computed in clean mode."""
        vr = self._vr(self._BASE)
        assert "U5a" not in vr.predicates
        assert "U5b" not in vr.predicates

    def test_clean_mode_set_correctly(self):
        vr = self._vr(self._BASE)
        assert vr.mode == "clean"

    def test_inv14_u6_u7_hold_after_successful_clean(self):
        """INV-14: after clean, U6/U7 hold (apps gone); U1-U4b reflect install still present."""
        vr = self._vr(self._BASE)
        assert vr.predicates["U6"] is True
        assert vr.predicates["U7"] is True

    def test_u6_containers_present_fails(self):
        seq = [_proc(0, "active"), _proc(1), _proc(1), _proc(0, "container-x"), _proc(0, "")]
        vr = self._vr(seq)
        assert vr.predicates["U6"] is False

    def test_u7_volumes_present_fails(self):
        seq = [_proc(0, "active"), _proc(1), _proc(1), _proc(0, ""), _proc(0, "vol-x")]
        vr = self._vr(seq)
        assert vr.predicates["U7"] is False
