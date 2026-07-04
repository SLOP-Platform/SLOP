"""installer/tests/test_user.py — unit tests for installer/user.py.

All getent and usermod/useradd I/O is mocked via ensure_user keyword-only
injection.  No real subprocess calls, no real user-database reads.

Idempotency contract source: ADR 0013 §5.

Coverage:
  TestEnsureUserCreation   — user absent → useradd called with correct identity
  TestEnsureUserPresent    — user present + correct attributes → no useradd
  TestEnsureUserMismatch   — user present + wrong attrs → InstallUserMismatchError
  TestEnsureUserDockerGroup — docker group absent/present; membership idempotency
  TestEnsureUserOrdering   — useradd before docker-group check
"""

from __future__ import annotations


import pytest

from installer._run import MissingBinaryError
from installer.user import (
    DockerGroupMissingError,
    InstallUserMismatchError,
    UserCreationError,
    _get_group_entry,
    _get_passwd_entry,
    _run_useradd,
    _run_usermod,
    check_existing_user_attrs,
    ensure_user,
)


# ── Shared helpers ────────────────────────────────────────────────────────────


def _noop(*_args, **_kwargs):
    pass


def _system_passwd(**overrides) -> dict:
    """A passwd entry with all-correct attributes (ADR 0013 §5)."""
    base = {"uid": 999, "shell": "/usr/sbin/nologin", "home": "/nonexistent"}
    base.update(overrides)
    return base


def _docker_group(members=None) -> dict:
    return {"name": "docker", "members": list(members or [])}


def _passing_kwargs(
    user_present: bool = False,
    user_in_group: bool = False,
    group_present: bool = True,
) -> dict:
    """Return ensure_user kwargs for a clean-install scenario by default."""
    passwd = _system_passwd() if user_present else None
    members = ["slop"] if user_in_group else []
    group = _docker_group(members) if group_present else None
    return {
        "get_passwd_entry": lambda username: passwd,
        "run_useradd": _noop,
        "get_group_entry": lambda group_name: group,
        "run_usermod": _noop,
    }


# ── TestEnsureUserCreation ────────────────────────────────────────────────────


class TestEnsureUserCreation:
    def test_user_absent_calls_useradd(self):
        calls = []
        kwargs = _passing_kwargs(user_present=False)
        kwargs["run_useradd"] = lambda u: calls.append(u)
        ensure_user(**kwargs)
        assert len(calls) == 1

    def test_useradd_receives_correct_username(self):
        usernames = []
        kwargs = _passing_kwargs(user_present=False)
        kwargs["run_useradd"] = lambda u: usernames.append(u)
        ensure_user(**kwargs)
        assert usernames == ["slop"]

    def test_custom_username_passed_to_useradd(self):
        usernames = []
        kwargs = _passing_kwargs(user_present=False)
        kwargs["run_useradd"] = lambda u: usernames.append(u)
        ensure_user(username="myservice", **kwargs)
        assert usernames == ["myservice"]

    def test_useradd_failure_propagates(self):
        def fail_add(u):
            raise UserCreationError("useradd exit 1")

        kwargs = _passing_kwargs(user_present=False)
        kwargs["run_useradd"] = fail_add
        with pytest.raises(UserCreationError):
            ensure_user(**kwargs)

    def test_useradd_failure_skips_docker_group(self):
        usermod_calls = []

        def fail_add(u):
            raise UserCreationError("failed")

        kwargs = _passing_kwargs(user_present=False)
        kwargs["run_useradd"] = fail_add
        kwargs["run_usermod"] = lambda u, g: usermod_calls.append((u, g))
        with pytest.raises(UserCreationError):
            ensure_user(**kwargs)
        assert usermod_calls == []


# ── TestEnsureUserPresent ─────────────────────────────────────────────────────


class TestEnsureUserPresent:
    def test_user_present_skips_useradd(self):
        calls = []
        kwargs = _passing_kwargs(user_present=True)
        kwargs["run_useradd"] = lambda u: calls.append(u)
        ensure_user(**kwargs)
        assert calls == []

    def test_correct_attributes_no_exception(self):
        kwargs = _passing_kwargs(user_present=True)
        ensure_user(**kwargs)  # must not raise

    def test_passwd_queried_with_correct_username(self):
        queried = []
        kwargs = _passing_kwargs(user_present=True)
        kwargs["get_passwd_entry"] = lambda u: queried.append(u) or _system_passwd()
        ensure_user(**kwargs)
        assert queried == ["slop"]

    def test_custom_username_queried(self):
        queried = []
        kwargs = _passing_kwargs(user_present=True)
        kwargs["get_passwd_entry"] = lambda u: queried.append(u) or _system_passwd()
        ensure_user(username="svc", **kwargs)
        assert queried == ["svc"]


# ── TestEnsureUserMismatch ────────────────────────────────────────────────────


class TestEnsureUserMismatch:
    def test_wrong_shell_raises_mismatch(self):
        kwargs = _passing_kwargs(user_present=True)
        kwargs["get_passwd_entry"] = lambda u: _system_passwd(shell="/bin/bash")
        with pytest.raises(InstallUserMismatchError):
            ensure_user(**kwargs)

    def test_wrong_home_raises_mismatch(self):
        kwargs = _passing_kwargs(user_present=True)
        kwargs["get_passwd_entry"] = lambda u: _system_passwd(home="/home/slop")
        with pytest.raises(InstallUserMismatchError):
            ensure_user(**kwargs)

    def test_non_system_uid_raises_mismatch(self):
        kwargs = _passing_kwargs(user_present=True)
        kwargs["get_passwd_entry"] = lambda u: _system_passwd(uid=1001)
        with pytest.raises(InstallUserMismatchError):
            ensure_user(**kwargs)

    def test_uid_at_ceiling_raises_mismatch(self):
        # UID == 1000 is not a system UID; must raise
        kwargs = _passing_kwargs(user_present=True)
        kwargs["get_passwd_entry"] = lambda u: _system_passwd(uid=1000)
        with pytest.raises(InstallUserMismatchError):
            ensure_user(**kwargs)

    def test_mismatch_error_message_names_uid(self):
        kwargs = _passing_kwargs(user_present=True)
        kwargs["get_passwd_entry"] = lambda u: _system_passwd(uid=1500)
        with pytest.raises(InstallUserMismatchError, match="1500"):
            ensure_user(**kwargs)

    def test_mismatch_error_message_names_shell(self):
        kwargs = _passing_kwargs(user_present=True)
        kwargs["get_passwd_entry"] = lambda u: _system_passwd(shell="/bin/sh")
        with pytest.raises(InstallUserMismatchError, match="/bin/sh"):
            ensure_user(**kwargs)

    def test_mismatch_error_message_names_home(self):
        kwargs = _passing_kwargs(user_present=True)
        kwargs["get_passwd_entry"] = lambda u: _system_passwd(home="/home/slop")
        with pytest.raises(InstallUserMismatchError, match="/home/slop"):
            ensure_user(**kwargs)

    def test_mismatch_skips_docker_group_check(self):
        group_calls = []
        kwargs = _passing_kwargs(user_present=True)
        kwargs["get_passwd_entry"] = lambda u: _system_passwd(shell="/bin/bash")
        kwargs["get_group_entry"] = lambda g: group_calls.append(g) or _docker_group()
        with pytest.raises(InstallUserMismatchError):
            ensure_user(**kwargs)
        assert group_calls == []


# ── TestEnsureUserDockerGroup ─────────────────────────────────────────────────


class TestEnsureUserDockerGroup:
    def test_docker_group_absent_raises(self):
        kwargs = _passing_kwargs(group_present=False)
        with pytest.raises(DockerGroupMissingError):
            ensure_user(**kwargs)

    def test_docker_group_absent_message_names_group(self):
        kwargs = _passing_kwargs(group_present=False)
        with pytest.raises(DockerGroupMissingError, match="docker"):
            ensure_user(**kwargs)

    def test_user_not_in_group_calls_usermod(self):
        calls = []
        kwargs = _passing_kwargs(user_in_group=False)
        kwargs["run_usermod"] = lambda u, g: calls.append((u, g))
        ensure_user(**kwargs)
        assert len(calls) == 1

    def test_usermod_receives_correct_username(self):
        calls = []
        kwargs = _passing_kwargs(user_in_group=False)
        kwargs["run_usermod"] = lambda u, g: calls.append((u, g))
        ensure_user(**kwargs)
        username, _ = calls[0]
        assert username == "slop"

    def test_usermod_receives_correct_group(self):
        calls = []
        kwargs = _passing_kwargs(user_in_group=False)
        kwargs["run_usermod"] = lambda u, g: calls.append((u, g))
        ensure_user(**kwargs)
        _, group = calls[0]
        assert group == "docker"

    def test_user_already_in_group_skips_usermod(self):
        calls = []
        kwargs = _passing_kwargs(user_in_group=True)
        kwargs["run_usermod"] = lambda u, g: calls.append((u, g))
        ensure_user(**kwargs)
        assert calls == []

    def test_group_queried_with_docker_group_name(self):
        groups_queried = []
        kwargs = _passing_kwargs()
        kwargs["get_group_entry"] = lambda g: groups_queried.append(g) or _docker_group()
        ensure_user(**kwargs)
        assert groups_queried == ["docker"]

    def test_custom_docker_group_queried(self):
        groups_queried = []
        kwargs = _passing_kwargs()
        kwargs["get_group_entry"] = lambda g: groups_queried.append(g) or _docker_group()
        ensure_user(docker_group="containerd", **kwargs)
        assert groups_queried == ["containerd"]


# ── TestEnsureUserOrdering ────────────────────────────────────────────────────


class TestEnsureUserOrdering:
    def test_useradd_before_docker_group(self):
        call_order = []
        kwargs = _passing_kwargs(user_present=False, user_in_group=False)
        kwargs["run_useradd"] = lambda u: call_order.append("useradd")
        kwargs["run_usermod"] = lambda u, g: call_order.append("usermod")
        ensure_user(**kwargs)
        assert call_order.index("useradd") < call_order.index("usermod")

    def test_full_fresh_install_order(self):
        call_order = []
        kwargs = _passing_kwargs(user_present=False, user_in_group=False)
        kwargs["run_useradd"] = lambda u: call_order.append("useradd")
        kwargs["run_usermod"] = lambda u, g: call_order.append("usermod")
        ensure_user(**kwargs)
        assert call_order == ["useradd", "usermod"]

    def test_idempotent_rerun_skips_useradd_and_usermod(self):
        call_order = []
        kwargs = _passing_kwargs(user_present=True, user_in_group=True)
        kwargs["run_useradd"] = lambda u: call_order.append("useradd")
        kwargs["run_usermod"] = lambda u, g: call_order.append("usermod")
        ensure_user(**kwargs)
        assert call_order == []


# ── TestCheckExistingUserAttrs ────────────────────────────────────────────────


class TestCheckExistingUserAttrs:
    """check_existing_user_attrs() — pre-flight read-only check (Ex 6.3 fix)."""

    def test_user_absent_no_raise(self):
        check_existing_user_attrs(get_passwd_entry=lambda u: None)

    def test_correct_attrs_no_raise(self):
        check_existing_user_attrs(get_passwd_entry=lambda u: _system_passwd())

    def test_wrong_shell_raises(self):
        with pytest.raises(InstallUserMismatchError):
            check_existing_user_attrs(get_passwd_entry=lambda u: _system_passwd(shell="/bin/bash"))

    def test_wrong_home_raises(self):
        with pytest.raises(InstallUserMismatchError):
            check_existing_user_attrs(get_passwd_entry=lambda u: _system_passwd(home="/home/slop"))

    def test_non_system_uid_raises(self):
        with pytest.raises(InstallUserMismatchError):
            check_existing_user_attrs(get_passwd_entry=lambda u: _system_passwd(uid=1001))

    def test_uid_at_ceiling_raises(self):
        with pytest.raises(InstallUserMismatchError):
            check_existing_user_attrs(get_passwd_entry=lambda u: _system_passwd(uid=1000))

    def test_mismatch_message_names_username(self):
        with pytest.raises(InstallUserMismatchError, match="slop"):
            check_existing_user_attrs(get_passwd_entry=lambda u: _system_passwd(uid=1500))

    def test_mismatch_message_names_bad_uid(self):
        with pytest.raises(InstallUserMismatchError, match="1500"):
            check_existing_user_attrs(get_passwd_entry=lambda u: _system_passwd(uid=1500))

    def test_custom_username_queried(self):
        queried = []
        check_existing_user_attrs(
            username="svc",
            get_passwd_entry=lambda u: queried.append(u) or None,
        )
        assert queried == ["svc"]


# ── TestUserBoundaryProbe ─────────────────────────────────────────────────────


class TestUserBoundaryProbe:
    """F8: boundary tests — run_required wraps FileNotFoundError → MissingBinaryError.

    getent / useradd / usermod are universally present on full Debian/Ubuntu
    installs but absent on minimal containers.  After migration, MissingBinaryError
    propagates when these binaries are missing.
    """

    def test_get_passwd_entry_raises_on_getent_absent(self):
        from unittest.mock import patch

        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("getent")):
            with pytest.raises(MissingBinaryError, match="getent"):
                _get_passwd_entry("slop")

    def test_run_useradd_raises_on_useradd_absent(self):
        from unittest.mock import patch

        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("useradd")):
            with pytest.raises(MissingBinaryError, match="useradd"):
                _run_useradd("slop")

    def test_get_group_entry_raises_on_getent_absent(self):
        from unittest.mock import patch

        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("getent")):
            with pytest.raises(MissingBinaryError, match="getent"):
                _get_group_entry("docker")

    def test_run_usermod_raises_on_usermod_absent(self):
        from unittest.mock import patch

        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("usermod")):
            with pytest.raises(MissingBinaryError, match="usermod"):
                _run_usermod("slop", "docker")
