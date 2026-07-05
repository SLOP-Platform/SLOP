"""installer/uninstall.py — uninstall, purge, and clean subcommand implementations.

Implements ADR 0017: docs/adr/0017-uninstall-semantics.md
Entry points: run_uninstall(), run_purge(), run_clean(), verify_removed().
All public functions use inject-kwargs for testability (Core Rule 5.27).
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from installer import post_install as _post_install_mod
from installer import state as _state_mod
from installer._defaults import DEFAULT_INSTALL_DIR as _DEFAULT_INSTALL_DIR
from installer._run import run_required
from installer.state import StateFile, StateFileCorruptedError, StateFileNewerSchemaError
from installer.uninstall_output import _clean_prompt, _print_clean_output

_SYSTEMD_UNIT = "/etc/systemd/system/slop.service"
# #868 P3 scheduled-backup units (advisory; may be absent on installs predating the timer or
# when its install best-effort-failed). Removed idempotently — see Step 4b.
_BACKUP_TIMER_UNIT = "/etc/systemd/system/ms-backup.timer"
_BACKUP_SERVICE_UNIT = "/etc/systemd/system/ms-backup.service"
_MS_USER = "slop"
_SYSTEM_UID_CEILING = 1000
_EXPECTED_SHELL = "/usr/sbin/nologin"
_EXPECTED_HOME = "/nonexistent"

# Directory into which deploy.sh symlinks the managed commands. Kept as a module
# constant (not hardcoded inline) so tests can redirect it away from the real
# system path — mirrors how _SYSTEMD_UNIT is patched in the test suite.
_LOCAL_BIN_DIR = "/usr/local/bin"

# The commands deploy.sh symlinks into _LOCAL_BIN_DIR (its Step 7 `ln -sf` blocks
# are the source of truth — keep this list in sync with deploy.sh §"Step 7").
# On uninstall/purge these symlinks would otherwise dangle (their targets vanish
# with the install dir). Defined ONCE here; the removal step and U8 both read it.
_MANAGED_SYMLINKS = ("ms-update", "ms-check", "slop-reality-probe", "ms")


# ── Exceptions ────────────────────────────────────────────────────────────────


class UninstallUserMismatchError(Exception):
    """§A.6 carve-out: slop user has unexpected attributes."""


class GroupHasUnexpectedMembersError(Exception):
    """§A.6.5 carve-out: slop group has members the installer did not add."""


# ── Structured results ────────────────────────────────────────────────────────


@dataclass
class RemovalVerification:
    """Return type of verify_removed(). Consumed by audit gate (ADR 0017 §A.7).

    predicates: per-predicate boolean (True = predicate holds).
    skipped: predicates omitted because a §A.6/§A.6.5 carve-out fired.
    diagnostics: per-predicate command for human investigation when False.
    """

    mode: str
    predicates: dict[str, bool]
    skipped: list[str]
    diagnostics: dict[str, str]


# ── Install-dir resolution (Core Rule 5.26) ───────────────────────────────────


def _resolve_install_dir(args) -> Path:
    """Resolve <install_dir> per ADR 0013 §1: flag > env > default."""
    raw = (
        getattr(args, "install_dir", None)
        or os.environ.get("MS_INSTALL_DIR")
        or _DEFAULT_INSTALL_DIR
    )
    return Path(raw)


# ── State-file read with §A.2 / §A.3 / §A.3.5 refusals ──────────────────────


def _read_state(
    install_dir: Path,
    *,
    state_read: Callable,
    err_fn: Callable,
) -> StateFile | None:
    """Read state file; emit refusal message and return None on error.

    Caller must return 1 when this returns None.
    """
    state_path = install_dir / _state_mod.STATE_FILE_NAME
    try:
        sf = state_read(state_path)
    except PermissionError:
        err_fn(
            f"State file at {state_path} exists but is not\n"
            "readable by the current user (Permission denied).\n\n"
            "The state file is mode 0640 owned by slop:slop. Run the\n"
            "uninstaller with sudo, or as a user in the slop group:\n\n"
            "    sudo slop <subcommand>"
        )
        return None
    except StateFileNewerSchemaError as exc:
        err_fn(str(exc))
        return None
    except StateFileCorruptedError as exc:
        err_fn(str(exc))
        return None

    if sf is None:
        err_fn(
            f"No v5 slop install detected at {install_dir}.\n\n"
            "If you have a v4.x install or a hand-rolled deployment, the v5 uninstaller\n"
            "cannot determine what to remove. Manual cleanup is documented in\n"
            'INSTALL.md (section "Removing pre-v5 installs").\n\n'
            "If you expected a v5 install here, check the --install-dir flag."
        )
        return None

    return sf


# ── §A.4 pipe-mode fail-fast ─────────────────────────────────────────────────


def _check_pipe_refusal(is_tty: bool, yes: bool, err_fn: Callable) -> bool:
    """Return True (refused) when pipe mode without --yes per §A.4 step 5."""
    if not is_tty and not yes:
        err_fn(
            "This is a destructive operation. To run non-interactively, pass --yes.\n"
            "To run interactively, run from a terminal."
        )
        return True
    return False


# ── §A.4 confirmation prompt ─────────────────────────────────────────────────


def _prompt_confirm(
    prompt_text: str,
    yes: bool,
    is_tty: bool,
    stdin_readline: Callable,
    print_fn: Callable,
) -> bool:
    """Return True if operator confirmed (or --yes bypassed). False = cancel."""
    if yes:
        return True
    print_fn(prompt_text)
    answer = stdin_readline().strip().lower()
    return answer in ("y", "yes")


# ── §A.6 / §A.6.5 user and group attribute checks ────────────────────────────


def _passwd_attrs(run: Callable, username: str) -> dict | None:
    """Return user attributes from getent passwd, or None if absent."""
    result = run(["getent", "passwd", username])
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split(":")
    if len(parts) < 7:
        return None
    try:
        uid = int(parts[2])
    except ValueError:
        uid = 9999
    return {"uid": uid, "home": parts[5], "shell": parts[6]}


def _group_members(run: Callable, group_name: str) -> list | None:
    """Return member list from getent group, or None if group absent."""
    result = run(["getent", "group", group_name])
    if result.returncode != 0:
        return None
    parts = result.stdout.strip().split(":")
    return [m for m in parts[3].split(",") if m] if len(parts) > 3 else []


# ── §B pipeline: shared uninstall/purge steps ────────────────────────────────


def _run_removal_pipeline(
    mode: str,
    install_dir: Path,
    data_dir: Path,
    sf: StateFile,
    *,
    run: Callable,
    print_fn: Callable,
    err_fn: Callable,
) -> int:
    """Execute §B.1 pipeline for 'uninstall' or 'purge'. Returns exit code.

    Pipeline order is part of the contract (§B.1 rationale). U1/U2/U3
    failures stop the pipeline. U4/U4b/U5b/U6/U7 failures are reported at
    end; the rest of the pipeline continues.
    """
    late_failures: list[str] = []

    # Step 3: stop service (U1)
    print_fn("Stopping slop.service...")
    result = run(["systemctl", "stop", "slop.service"])
    if result.returncode != 0:
        # Soft-fail if the unit file is already absent (e.g., running purge after
        # a prior uninstall removed the unit). An absent unit cannot be running.
        unit_check = run(["test", "-e", _SYSTEMD_UNIT])
        if unit_check.returncode != 0:
            pass  # Unit file absent — service cannot be running; treat stop as no-op
        else:
            err_fn(
                "The slop service did not stop. The unit may have a `KillMode=`\n"
                "configuration that prevents clean shutdown, or a child process may be unkillable.\n"
                "Diagnostic: `systemctl status slop.service` and `ps -ef | grep slop`"
            )
            return 1  # U1 fail → pipeline stops

    # Step 4: disable service (idempotent; ignore exit code)
    run(["systemctl", "disable", "slop.service"])

    # Step 4b: tear down the #868 P3 scheduled-backup units (idempotent, best-effort).
    # Advisory units that may be absent (installs predating the timer, or a best-effort
    # install-time failure); every step ignores its exit code so a missing unit is a no-op.
    print_fn("Removing scheduled-backup timer (ms-backup.timer)...")
    run(["systemctl", "stop", "ms-backup.timer"])
    run(["systemctl", "disable", "ms-backup.timer"])
    run(["rm", "-f", _BACKUP_TIMER_UNIT])
    run(["rm", "-f", _BACKUP_SERVICE_UNIT])

    # Step 5: remove unit file + daemon-reload (U2)
    print_fn(f"Removing systemd unit {_SYSTEMD_UNIT}...")
    result = run(["rm", "-f", _SYSTEMD_UNIT])
    if result.returncode != 0:
        err_fn(
            "The systemd unit file could not be removed. The path may be a read-only\n"
            "mount, a symlink to a different file, or held by another tool.\n"
            f"Diagnostic: `ls -la {_SYSTEMD_UNIT}` and `mount | grep '/etc'`"
        )
        return 1  # U2 fail → pipeline stops
    run(["systemctl", "daemon-reload"])  # Best-effort; continue regardless

    # Step 5b: remove the /usr/local/bin command symlinks deploy.sh created (U8).
    # These point INTO the install dir, so once Step 6 removes it they would
    # dangle. Remove them here so no uninstall path leaves a broken link. Each is
    # removed only if it IS a symlink (`test -L` — true even for a dangling link,
    # unlike `test -e`); a real file of the same name is left untouched. Absent =
    # no-op (idempotent). `rm -f` failure is a late failure (U8), not fatal.
    _remove_managed_symlinks(run, print_fn, late_failures)

    # Step 6: remove install dir (U3)
    print_fn(f"Removing install directory {install_dir}...")
    result = run(["rm", "-rf", str(install_dir)])
    if result.returncode != 0:
        err_fn(
            "The install directory could not be fully removed. A file may be in use,\n"
            "the filesystem may have immutable-bit-set files, or a child mount may be present.\n"
            f"Diagnostic: `lsof +D {install_dir}` and `mount | grep {install_dir}`"
        )
        return 1  # U3 fail → pipeline stops

    # Step 7: remove user (§A.6 carve-out)
    # userdel is skipped when the user is already absent — the user may have been
    # removed by a prior uninstall. _passwd_attrs returns None when user absent.
    _usr = _passwd_attrs(run, _MS_USER)
    if _usr is not None:
        uid, shell, home = _usr["uid"], _usr["shell"], _usr["home"]
        if uid >= _SYSTEM_UID_CEILING or shell != _EXPECTED_SHELL or home != _EXPECTED_HOME:
            err_fn(
                f"User '{_MS_USER}' exists but has unexpected attributes:\n"
                f"  UID:   {uid}     (expected: system UID < {_SYSTEM_UID_CEILING})\n"
                f"  Shell: {shell!r}     (expected: {_EXPECTED_SHELL!r})\n"
                f"  Home:  {home!r}     (expected: {_EXPECTED_HOME!r})\n\n"
                "The installer will not remove a user it did not install. If this user\n"
                "was created by the installer and modified afterwards, remove it manually:\n\n"
                f"    sudo userdel {_MS_USER}\n\n"
                "Continuing uninstall (other removal steps proceed)."
            )
        else:
            result = run(["userdel", _MS_USER])
            if result.returncode != 0:
                err_fn(
                    "The `slop` user could not be removed. The user may have a running\n"
                    "process (login session, cron job, lingering systemd user services), or a\n"
                    "system policy may be blocking `userdel`.\n"
                    "Diagnostic: `loginctl user-status slop` and `ps -u slop`"
                )
                late_failures.append("U4")
    # else: user already absent — skip userdel

    # Step 8: remove group (§A.6.5 carve-out) — uninstall ends here
    # userdel (step 7) on some distros (e.g. Ubuntu) removes the user-private
    # primary group automatically when the user is deleted. Re-check group
    # existence before calling groupdel to avoid a spurious late failure.
    _grp = _group_members(run, _MS_USER)
    if _grp is not None:
        extra = [m for m in _grp if m != sf.install_user]
        if extra:
            err_fn(
                f"Group '{_MS_USER}' exists with members not added by installer:\n"
                f"  {', '.join(extra)}\n\n"
                "The installer will not remove a group it did not solely populate.\n"
                "These additional members may belong to third-party tooling. Remove\n"
                "the group manually after verifying its membership:\n\n"
                f"    sudo groupdel {_MS_USER}\n\n"
                "Continuing uninstall (other removal steps proceed)."
            )
        else:
            result = run(["groupdel", _MS_USER])
            if result.returncode != 0:
                err_fn(
                    "The `slop` group could not be removed.\n"
                    "Diagnostic: `getent group slop` and `journalctl -t groupdel -n 50`"
                )
                late_failures.append("U4b")
    # else: group already absent — userdel cleaned it up; groupdel not needed

    if mode == "uninstall":
        if late_failures:
            return 1
        print_fn("Uninstall complete.")
        return 0

    # ── Purge-only: steps 9-11 ────────────────────────────────────────────────

    # Step 9: rm -rf <data_dir> with TOCTOU guard (ADR 0017 §S.1)
    print_fn(f"Removing data directory {data_dir}...")
    real_data = data_dir.resolve()
    state_data = Path(sf.data_dir).resolve()
    if real_data != state_data:
        err_fn(
            f"Data directory path mismatch (symlink safety): "
            f"resolved {real_data} != state-file {state_data}. "
            "Refusing to remove. Investigate and remove manually."
        )
        late_failures.append("U5b")
    else:
        result = run(["rm", "-rf", "--one-file-system", str(real_data)])
        if result.returncode != 0:
            err_fn(
                "The data directory could not be fully removed. A file may be in use,\n"
                "the filesystem may have immutable-bit-set files, or the dir may be a\n"
                "mount point with content provided by another tool.\n"
                f"Diagnostic: `lsof +D {data_dir}` and `mount | grep {data_dir}`"
            )
            late_failures.append("U5b")

    # Step 10: docker rm managed containers (U6)
    print_fn("Removing managed Docker containers...")
    container_ids = _enumerate_docker_objects(
        run,
        ["docker", "ps", "-a", "--filter", "label=slop.managed=true", "--format", "{{.ID}}"],
        err_fn,
        "U6",
        "The Docker daemon is not accessible. Without daemon access, managed\n"
        "containers cannot be enumerated or removed.\n"
        "Diagnostic: `systemctl status docker` and `docker info`",
        late_failures,
    )
    if container_ids is not None and container_ids:
        result = run(["docker", "rm", "-f", *container_ids])
        if result.returncode != 0:
            err_fn(
                "Managed containers could not all be removed. Some may have failed\n"
                "`docker rm` due to volume mount issues, network conflicts, or daemon errors.\n"
                "Diagnostic: `docker ps -a --filter label=slop.managed=true` "
                "and `journalctl -u docker -n 50`"
            )
            late_failures.append("U6")

    # Step 11: docker volume rm managed volumes (U7)
    print_fn("Removing managed Docker volumes...")
    volume_names = _enumerate_docker_objects(
        run,
        ["docker", "volume", "ls", "--filter", "label=slop.managed=true", "--format", "{{.Name}}"],
        err_fn,
        "U7",
        "The Docker daemon is not accessible for volume enumeration.\n"
        "Diagnostic: `systemctl status docker` and `docker info`",
        late_failures,
    )
    if volume_names is not None and volume_names:
        result = run(["docker", "volume", "rm", *volume_names])
        if result.returncode != 0:
            err_fn(
                "Managed volumes could not all be removed. Some may have failed\n"
                "`docker volume rm` due to ongoing use or driver-specific errors.\n"
                "Diagnostic: inspect each remaining volume with `docker volume inspect <name>`"
            )
            late_failures.append("U7")

    if late_failures:
        return 1
    print_fn("Purge complete.")
    return 0


def _remove_managed_symlinks(
    run: Callable,
    print_fn: Callable,
    late_failures: list,
) -> None:
    """Remove deploy.sh's /usr/local/bin command symlinks (U8); idempotent.

    Only removes a path that IS a symlink (`test -L`) so a real file of the same
    name is never clobbered; absent or non-symlink paths are skipped. An `rm`
    failure appends "U8" to late_failures (reported at pipeline end, non-fatal).
    Source of truth for the name list is _MANAGED_SYMLINKS / deploy.sh Step 7.
    """
    print_fn(f"Removing managed command symlinks in {_LOCAL_BIN_DIR}...")
    for name in _MANAGED_SYMLINKS:
        link = f"{_LOCAL_BIN_DIR}/{name}"
        if run(["test", "-L", link]).returncode != 0:
            continue  # Not a symlink (absent, or a real file we must not touch)
        if run(["rm", "-f", link]).returncode != 0:
            err_msg = (
                f"The managed symlink {link} could not be removed. The path may be\n"
                "on a read-only mount or held by another tool.\n"
                f"Diagnostic: `ls -la {link}`"
            )
            print_fn(err_msg)
            if "U8" not in late_failures:
                late_failures.append("U8")


def _enumerate_docker_objects(
    run: Callable,
    cmd: list,
    err_fn: Callable,
    predicate: str,
    daemon_error_msg: str,
    late_failures: list,
) -> list | None:
    """Run a docker enumeration command; return items or None on daemon error."""
    result = run(cmd)
    if result.returncode != 0:
        err_fn(daemon_error_msg)
        late_failures.append(predicate)
        return None
    return [item for item in result.stdout.strip().splitlines() if item]


# ── §B.6 confirmation prompt text builders ───────────────────────────────────


def _uninstall_prompt(sf: StateFile, install_dir: Path, data_dir: Path) -> str:
    return (
        f"About to uninstall slop v{sf.slop_version}:\n\n"
        f"  - Stop and disable slop.service\n"
        f"  - Remove {_SYSTEMD_UNIT}\n"
        f"  - Remove {install_dir} (code, venv, frontend, state file, POST_INSTALL.txt)\n"
        f"  - Remove user 'slop' and group 'slop' (if installer-installed)\n\n"
        f"The data directory {data_dir} will be PRESERVED.\n"
        "To also remove data and managed containers, use 'slop purge' instead.\n\n"
        "Continue? [y/N]: "
    )


def _purge_prompt(sf: StateFile, install_dir: Path, data_dir: Path, hostname: str) -> str:
    return (
        f"About to PURGE slop v{sf.slop_version} from {hostname}:\n\n"
        f"  - Stop and disable slop.service\n"
        f"  - Remove {_SYSTEMD_UNIT}\n"
        f"  - Remove {install_dir} (code, venv, frontend, state file, POST_INSTALL.txt)\n"
        f"  - Remove user 'slop' and group 'slop' (if installer-installed)\n"
        f"  - Remove {data_dir} (state.db, .env, per-app configs, compose fragments)\n"
        f"  - Remove ALL slop-managed Docker containers (label: slop.managed=true)\n"
        f"  - Remove ALL slop-managed Docker volumes (label: slop.managed=true)\n\n"
        "This operation is IRREVERSIBLE. All app data will be lost.\n\n"
        "Continue? [y/N]: "
    )


# ── verify_removed (§A.7) ─────────────────────────────────────────────────────


def verify_removed(
    install_dir: str | Path,
    data_dir: str | Path,
    mode: str,
    *,
    run: Callable = run_required,
    pre_data_dir_stat: os.stat_result | None = None,
) -> RemovalVerification:
    """Check U-predicates against post-action filesystem state.

    Pure function (no removals). mode: 'uninstall' | 'purge' | 'clean'.
    pre_data_dir_stat: snapshot taken before the pipeline for U5a (uninstall).
    """
    install_dir = Path(install_dir)
    data_dir = Path(data_dir)

    predicates: dict[str, bool] = {}
    skipped: list[str] = []
    diagnostics: dict[str, str] = {
        "U1": "systemctl status slop.service",
        "U2": f"ls -la {_SYSTEMD_UNIT}",
        "U3": f"lsof +D {install_dir}",
        "U4": "loginctl user-status slop && ps -u slop",
        "U4b": "getent group slop && journalctl -t groupdel -n 50",
        "U5a": f"lsof +D {data_dir} (check for mutation, not removal)",
        "U5b": f"lsof +D {data_dir} && mount | grep {data_dir}",
        "U6": "docker ps -a --filter label=slop.managed=true",
        "U7": "docker volume ls --filter label=slop.managed=true",
        "U8": f"ls -la {' '.join(_LOCAL_BIN_DIR + '/' + n for n in _MANAGED_SYMLINKS)}",
    }

    # U1: service inactive or unknown
    result = run(["systemctl", "is-active", "slop.service"])
    svc_status = result.stdout.strip()
    predicates["U1"] = result.returncode != 0 and svc_status in ("inactive", "unknown", "")

    # U2: unit file absent
    predicates["U2"] = not Path(_SYSTEMD_UNIT).exists()

    # U3: install dir absent
    predicates["U3"] = not install_dir.exists()

    # U4: user absent (or §A.6 carve-out)
    result_u4 = run(["getent", "passwd", _MS_USER])
    if result_u4.returncode != 0:
        predicates["U4"] = True  # User gone — predicate holds
    else:
        parts = result_u4.stdout.strip().split(":")
        uid = int(parts[2]) if len(parts) > 2 else 9999
        shell = parts[6] if len(parts) > 6 else ""
        home = parts[5] if len(parts) > 5 else ""
        if mode in ("uninstall", "purge") and (
            uid >= _SYSTEM_UID_CEILING or shell != _EXPECTED_SHELL or home != _EXPECTED_HOME
        ):
            skipped.append("U4")  # §A.6 carve-out: installer wouldn't have removed
        else:
            predicates["U4"] = False  # User still present

    # U4b: group absent or only expected member (or §A.6.5 carve-out)
    result_u4b = run(["getent", "group", _MS_USER])
    if result_u4b.returncode != 0:
        predicates["U4b"] = True  # Group gone
    else:
        parts = result_u4b.stdout.strip().split(":")
        members = [m for m in parts[3].split(",") if m] if len(parts) > 3 else []
        extra = [m for m in members if m != _MS_USER]
        if extra and mode in ("uninstall", "purge"):
            skipped.append("U4b")  # §A.6.5 carve-out
        else:
            # Group gone or only slop user (or clean mode) — acceptable per §B.2
            predicates["U4b"] = len(extra) == 0

    # U5a (uninstall only): data dir preserved, inode and mtime unchanged
    if mode == "uninstall":
        if pre_data_dir_stat is not None:
            try:
                post_stat = data_dir.stat()
                predicates["U5a"] = (
                    post_stat.st_ino == pre_data_dir_stat.st_ino
                    and abs(post_stat.st_mtime - pre_data_dir_stat.st_mtime) < 0.001
                )
            except OSError:
                predicates["U5a"] = False  # Stat failed
        else:
            predicates["U5a"] = data_dir.exists()  # Fallback: at least check presence

    # U5b (purge only): data dir absent
    if mode == "purge":
        predicates["U5b"] = not data_dir.exists()

    # U6: no managed containers (purge and clean)
    if mode in ("purge", "clean"):
        result = run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                "label=slop.managed=true",
                "--format",
                "{{.Names}}",
            ]
        )
        predicates["U6"] = result.returncode == 0 and not result.stdout.strip()

    # U7: no managed volumes (purge and clean)
    if mode in ("purge", "clean"):
        result = run(
            [
                "docker",
                "volume",
                "ls",
                "--filter",
                "label=slop.managed=true",
                "--format",
                "{{.Name}}",
            ]
        )
        predicates["U7"] = result.returncode == 0 and not result.stdout.strip()

    # U8 (uninstall and purge): none of deploy.sh's /usr/local/bin command
    # symlinks remain. Excluded from clean mode, which leaves the install dir (and
    # thus the still-valid symlinks) in place. A real file of the same name does
    # not violate U8 — only a lingering SYMLINK does (that is what would dangle).
    if mode in ("uninstall", "purge"):
        predicates["U8"] = not any(
            Path(f"{_LOCAL_BIN_DIR}/{name}").is_symlink() for name in _MANAGED_SYMLINKS
        )

    return RemovalVerification(
        mode=mode,
        predicates=predicates,
        skipped=skipped,
        diagnostics=diagnostics,
    )


# ── Clean: §C.2/§C.3 container enumeration ───────────────────────────────────


def _enumerate_managed_apps(
    run: Callable,
) -> tuple[list[str], list[str]] | None:
    """Enumerate managed containers; return (app_keys, orphan_names) or None on error.

    Returns None when the Docker daemon is unreachable (§C.2 fail-fast).
    """
    result = run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            "label=slop.managed=true",
            "--format",
            '{{.Names}}\t{{.Label "slop.app-key"}}',
        ]
    )
    if result.returncode != 0:
        return None

    seen_keys: list[str] = []
    orphans: list[str] = []
    for line in result.stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split("\t", 1)
        name = parts[0].strip()
        key = parts[1].strip() if len(parts) > 1 else ""
        if key:
            if key not in seen_keys:
                seen_keys.append(key)
        else:
            orphans.append(name)

    return seen_keys, orphans


# ── Clean: §C.1 backend HTTP API call ────────────────────────────────────────


def _api_remove_app(key: str, port: int) -> dict:
    """Call DELETE /api/apps/{key} with delete_config=true. Returns JSON response dict."""
    url = f"http://127.0.0.1:{port}/api/apps/{key}"
    body = json.dumps({"delete_config": True}).encode()
    req = urllib.request.Request(url, data=body, method="DELETE")  # noqa: S310 — installer fetch of a known/operator-configured URL over urllib; single-trusted-operator threat model
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:  # noqa: S310 — installer fetch of a known/operator-configured URL over urllib; single-trusted-operator threat model
            return json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        return {
            "ok": False,
            "app_key": key,
            "operation": "remove",
            "steps": [],
            "error": f"HTTP {exc.code}: {exc.reason}",
        }
    except Exception as exc:
        return {
            "ok": False,
            "app_key": key,
            "operation": "remove",
            "steps": [],
            "error": str(exc),
        }


# ── Public entry points ───────────────────────────────────────────────────────


def run_uninstall(
    args,
    *,
    state_read: Callable = _state_mod.read_state_file,
    stdin_is_tty: Callable = sys.stdin.isatty,
    stdin_readline: Callable = sys.stdin.readline,
    resolve_hostname: Callable = _post_install_mod._resolve_hostname,
    run: Callable = run_required,
    print_fn: Callable = print,
    err_fn: Callable | None = None,
) -> int:
    """Implement the 'uninstall' subcommand per ADR 0017 §B."""
    if err_fn is None:
        err_fn = lambda msg: print(msg, file=sys.stderr)  # noqa: E731

    # Steps 1-3 (§A.4): args parsed, TTY detected, install_dir resolved
    install_dir = _resolve_install_dir(args)
    is_tty = stdin_is_tty()

    # Step 4: read state file
    sf = _read_state(install_dir, state_read=state_read, err_fn=err_fn)
    if sf is None:
        return 1

    # Step 5: pipe-mode refusal without --yes
    if _check_pipe_refusal(is_tty, args.yes, err_fn):
        return 1

    # Step 6: confirmation prompt
    data_dir = Path(sf.data_dir)
    prompt = _uninstall_prompt(sf, install_dir, data_dir)
    if not _prompt_confirm(prompt, args.yes, is_tty, stdin_readline, print_fn):
        print_fn("Uninstall cancelled.")
        return 0

    # Steps 3-8: pipeline (first side effect)
    return _run_removal_pipeline(
        "uninstall",
        install_dir,
        data_dir,
        sf,
        run=run,
        print_fn=print_fn,
        err_fn=err_fn,
    )


def run_purge(
    args,
    *,
    state_read: Callable = _state_mod.read_state_file,
    stdin_is_tty: Callable = sys.stdin.isatty,
    stdin_readline: Callable = sys.stdin.readline,
    resolve_hostname: Callable = _post_install_mod._resolve_hostname,
    run: Callable = run_required,
    print_fn: Callable = print,
    err_fn: Callable | None = None,
) -> int:
    """Implement the 'purge' subcommand per ADR 0017 §B."""
    if err_fn is None:
        err_fn = lambda msg: print(msg, file=sys.stderr)  # noqa: E731

    install_dir = _resolve_install_dir(args)
    is_tty = stdin_is_tty()

    sf = _read_state(install_dir, state_read=state_read, err_fn=err_fn)
    if sf is None:
        return 1

    if _check_pipe_refusal(is_tty, args.yes, err_fn):
        return 1

    data_dir = Path(sf.data_dir)
    hostname = resolve_hostname()
    prompt = _purge_prompt(sf, install_dir, data_dir, hostname)
    if not _prompt_confirm(prompt, args.yes, is_tty, stdin_readline, print_fn):
        print_fn("Purge cancelled.")
        return 0

    return _run_removal_pipeline(
        "purge",
        install_dir,
        data_dir,
        sf,
        run=run,
        print_fn=print_fn,
        err_fn=err_fn,
    )


def run_clean(
    args,
    *,
    state_read: Callable = _state_mod.read_state_file,
    stdin_is_tty: Callable = sys.stdin.isatty,
    stdin_readline: Callable = sys.stdin.readline,
    resolve_hostname: Callable = _post_install_mod._resolve_hostname,
    run: Callable = run_required,
    api_remove: Callable = _api_remove_app,
    print_fn: Callable = print,
    err_fn: Callable | None = None,
) -> int:
    """Implement the 'clean' subcommand per ADR 0017 §C."""
    if err_fn is None:
        err_fn = lambda msg: print(msg, file=sys.stderr)  # noqa: E731

    install_dir = _resolve_install_dir(args)
    is_tty = stdin_is_tty()

    # Step 4: read state file
    sf = _read_state(install_dir, state_read=state_read, err_fn=err_fn)
    if sf is None:
        return 1

    # Step 5: pipe-mode refusal without --yes
    if _check_pipe_refusal(is_tty, args.yes, err_fn):
        return 1

    # §C.4 pre-condition: service must be active
    result = run(["systemctl", "is-active", "slop.service"])
    if result.returncode != 0 or result.stdout.strip() != "active":
        err_fn(
            "The slop service is not running. The 'clean' subcommand requires\n"
            "the service active to dispatch per-app removal through the backend.\n\n"
            "Start the service first:\n\n"
            "    sudo systemctl start slop.service\n\n"
            "Or use 'uninstall' / 'purge' for full removal that doesn't require the\n"
            "service running."
        )
        return 1

    # §C.2/§C.3: enumerate managed containers (read-only; before confirmation)
    enumeration = _enumerate_managed_apps(run)
    if enumeration is None:
        err_fn(
            "The Docker daemon is not accessible. The 'clean' subcommand cannot\n"
            "enumerate managed containers without Docker.\n"
            "Diagnostic: `systemctl status docker` and `docker info`"
        )
        return 1

    app_keys, orphans = enumeration
    data_dir = Path(sf.data_dir)
    port = sf.port
    hostname = resolve_hostname()

    if not app_keys and not orphans:
        print_fn("No managed apps found. Nothing to clean.")
        return 0

    # Step 6: confirmation prompt (lists apps per §C.7)
    prompt = _clean_prompt(app_keys, orphans, hostname, port, data_dir)
    if not _prompt_confirm(prompt, args.yes, is_tty, stdin_readline, print_fn):
        print_fn("Clean cancelled.")
        return 0

    # Step 7+: call backend API per app (first real side effect)
    results: list[tuple[str, dict]] = []
    for key in app_keys:
        result_data = api_remove(key, port)
        results.append((key, result_data))

    exit_code = _print_clean_output(results, orphans, print_fn)

    # §C.8: post-output URL on success or warnings-only
    has_failed = any(r.get("ok") is False for _, r in results)
    if not has_failed:
        print_fn(f"\nSLOP remains running at http://{hostname}:{port}/")

    return exit_code
