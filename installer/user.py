"""installer/user.py — system user provisioning step for the v5 installer.

ensure_user() creates the unprivileged `slop` system user per
ADR 0013 §5 and adds it to the `docker` group.  The function is idempotent:
if the user already exists with the expected attributes it is verified and
kept; mismatched attributes raise InstallUserMismatchError rather than
silently modifying an account the installer did not create.
"""

from __future__ import annotations

from collections.abc import Callable

from installer._run import run_required


# ── Error classes (per ADR 0013 §5 idempotency contract) ─────────────────────


class UserError(Exception):
    pass


class UserCreationError(UserError):
    pass


class InstallUserMismatchError(UserError):
    pass


class DockerGroupMissingError(UserError):
    pass


# ── Attribute constants (ADR 0013 §5 attribute table) ─────────────────────────

_EXPECTED_SHELL: str = "/usr/sbin/nologin"
_EXPECTED_HOME: str = "/nonexistent"
_SYSTEM_UID_CEILING: int = 1000  # system UIDs are < 1000


# ── I/O helpers (replaceable in tests via ensure_user kwargs) ─────────────────


def _get_passwd_entry(username: str) -> dict | None:
    """Return passwd attributes for username, or None if the user does not exist."""
    result = run_required(["getent", "passwd", username])
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split(":")
    return {
        "uid": int(parts[2]),
        "home": parts[5],
        "shell": parts[6],
    }


def _run_useradd(username: str) -> None:
    result = run_required(
        [
            "useradd",
            "--system",
            "--user-group",
            "--no-create-home",
            "--home-dir",
            _EXPECTED_HOME,
            "--shell",
            _EXPECTED_SHELL,
            "--comment",
            "slop service account",
            username,
        ]
    )
    if result.returncode != 0:
        raise UserCreationError(f"useradd failed for {username!r}: {result.stderr.strip()}")


def _get_group_entry(group_name: str) -> dict | None:
    """Return group attributes (including members list) or None if absent."""
    result = run_required(["getent", "group", group_name])
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split(":")
    members = [m for m in parts[3].split(",") if m] if len(parts) > 3 else []
    return {"name": parts[0], "members": members}


def _run_usermod(username: str, group_name: str) -> None:
    result = run_required(["usermod", "--append", "--groups", group_name, username])
    if result.returncode != 0:
        raise UserError(
            f"usermod --append --groups {group_name} {username} failed: {result.stderr.strip()}"
        )


# ── Public entry points ───────────────────────────────────────────────────────


def check_existing_user_attrs(
    *,
    username: str = "slop",
    get_passwd_entry: Callable[[str], dict | None] = _get_passwd_entry,
) -> None:
    """Pre-flight read-only check: if username exists, verify its attributes.

    Called before any filesystem writes.  If the user is absent, this is a
    no-op — user creation is a pipeline step.  If the user is present with
    unexpected attributes, raises InstallUserMismatchError so the operator
    gets a clean refusal before the install dir or state file are created.
    """
    passwd = get_passwd_entry(username)
    if passwd is None:
        return
    uid = passwd["uid"]
    shell = passwd["shell"]
    home = passwd["home"]
    if uid >= _SYSTEM_UID_CEILING or shell != _EXPECTED_SHELL or home != _EXPECTED_HOME:
        raise InstallUserMismatchError(
            f"User `{username}` exists but has unexpected attributes "
            f"(UID={uid}, shell={shell!r}, home={home!r}; "
            f"expected system UID <{_SYSTEM_UID_CEILING}, "
            f"{_EXPECTED_SHELL!r}, {_EXPECTED_HOME!r}). "
            "The installer will not modify an existing user account; "
            "remove or rename the user manually before reinstalling."
        )


def ensure_user(
    *,
    username: str = "slop",
    docker_group: str = "docker",
    get_passwd_entry: Callable[[str], dict | None] = _get_passwd_entry,
    run_useradd: Callable[[str], None] = _run_useradd,
    get_group_entry: Callable[[str], dict | None] = _get_group_entry,
    run_usermod: Callable[[str, str], None] = _run_usermod,
) -> None:
    """Create the slop system user and add it to the docker group.

    Implements the idempotency contract from ADR 0013 §5:

    1. If the user is absent: run useradd with --system --user-group
       --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin.
    2. If the user is present: verify UID < 1000, shell=/usr/sbin/nologin,
       home=/nonexistent.  Any mismatch raises InstallUserMismatchError —
       the installer never silently modifies an existing account.
    3. If the docker group is absent: raise DockerGroupMissingError.  Docker
       must be installed (ensure_docker()) before user provisioning.
    4. If the user is not yet a member of the docker group: run usermod.
       If already a member: no-op (idempotent re-run path).

    The keyword-only I/O arguments exist solely for unit-test injection;
    production callers omit them and get the real system calls.
    """
    # Steps 1-2: user existence and attribute check
    passwd = get_passwd_entry(username)
    if passwd is None:
        run_useradd(username)
    else:
        uid = passwd["uid"]
        shell = passwd["shell"]
        home = passwd["home"]
        if uid >= _SYSTEM_UID_CEILING or shell != _EXPECTED_SHELL or home != _EXPECTED_HOME:
            raise InstallUserMismatchError(
                f"User `{username}` exists but has unexpected attributes "
                f"(UID={uid}, shell={shell!r}, home={home!r}; "
                f"expected system UID <{_SYSTEM_UID_CEILING}, "
                f"{_EXPECTED_SHELL!r}, {_EXPECTED_HOME!r}). "
                "The installer will not modify an existing user account; "
                "remove or rename the user manually before reinstalling."
            )

    # Steps 3-4: docker group membership
    group = get_group_entry(docker_group)
    if group is None:
        raise DockerGroupMissingError(
            f"The `{docker_group}` group does not exist. "
            "Docker must be installed (installer/docker.py::ensure_docker()) "
            "before user provisioning."
        )

    if username not in group["members"]:
        run_usermod(username, docker_group)
