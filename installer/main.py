#!/usr/bin/env python3
"""installer/main.py — v5 installer dispatcher.

Parses the top-level subcommand and delegates to the appropriate handler.
Tier 1 stubs land in Tier 2+; _cmd_install is fully wired as of Step 2.7.5.
Step 3.2.b extends run_install_pipeline with smoke test and post_install.
"""

import argparse
import dataclasses
import datetime
import os
import shutil
import sys
import threading
from pathlib import Path
from collections.abc import Callable

from installer import (
    _run as _run_mod,
    backend,
    data_dir as _data_dir_mod,
    deps_debian,
    detect as _detect_mod,
    docker,
    fetch,
    frontend,
    install as _install_mod,
    post_install as _post_install_mod,
    prereq,
    service,
    smoke as _smoke_mod,
    state,
    uninstall as _uninstall_mod,
    user,
)

_DEFAULT_PORT: int = 8080
# Canonical product version lives in backend/__init__.py::__version__. The installer
# keeps its own literal (it runs in a bootstrap context where backend isn't importable)
# and prefers the resolved release-tag version at runtime; keep this in sync on release.
_INSTALLER_VERSION: str = "5.1.0"


# ── Parallel step runner ──────────────────────────────────────────────────────


def _run_parallel(fn_a: Callable, fn_b: Callable) -> None:
    """Run two zero-argument callables concurrently in daemon threads.

    Both callables are started simultaneously and joined before returning.
    If either raises an exception, the first exception captured is re-raised
    after both threads complete (so the second step always runs to completion
    rather than being abandoned mid-way).

    Thread safety: Python's list.append is GIL-protected; _errors is safe to
    write from multiple threads.  The external command inside each callable is
    also thread-safe (each call creates its own child process with its own fds).
    """
    _errors: list[BaseException] = []

    def _wrap(fn: Callable) -> None:
        try:
            fn()
        except Exception as exc:
            _errors.append(exc)

    t_a = threading.Thread(target=_wrap, args=(fn_a,), daemon=True)
    t_b = threading.Thread(target=_wrap, args=(fn_b,), daemon=True)
    t_a.start()
    t_b.start()
    t_a.join()
    t_b.join()

    if _errors:
        raise _errors[0]


# ── Consent resolution (ADR 0013 §3) ─────────────────────────────────────────


def _resolve_consent_mode(
    args: argparse.Namespace,
    stdin_is_tty: Callable[[], bool],
) -> str | None:
    """Map --install-docker flag + TTY state to consent_mode for ensure_docker().

    Returns "yes", "no", or None (interactive).  Raises RuntimeError for the
    pipe-mode-without-flag case that install.sh should have caught first.
    """
    install_docker = getattr(args, "install_docker", None)
    if install_docker == "yes":
        return "yes"
    if install_docker == "no":
        return "no"
    # Flag not set: valid only in interactive (TTY) mode per ADR 0013 §3
    if stdin_is_tty():
        return None  # docker.ensure_docker will prompt
    raise RuntimeError(
        "stdin is not a TTY and --install-docker is not set. "
        "This should have been caught by install.sh (pipe-mode fail-fast). "
        "Re-run with --install-docker=yes or --install-docker=no."
    )


# ── Install pipeline (Step 2.7.5) ─────────────────────────────────────────────


def _default_setup_community_dir(install_dir_path: Path) -> None:
    """Pre-create catalog/community/ with slop ownership.

    The clone does not create this directory (git omits empty dirs).
    The slop user must exist at call time (user_install ran before fetch_repo).
    """
    community_dir = install_dir_path / "catalog" / "community"
    community_dir.mkdir(parents=True, exist_ok=True)
    chown_result = _run_mod.run_required(
        ["chown", "slop:slop", str(community_dir)],
    )
    if chown_result.returncode != 0:
        raise Exception(f"chown slop:slop {community_dir} failed: {chown_result.stderr.strip()}")


def run_install_pipeline(
    args: argparse.Namespace,
    *,
    state_read: Callable = state.read_state_file,
    state_write: Callable = state.write_state_file,
    prereq_check: Callable = prereq.check_prereqs,
    detect_os: Callable = _detect_mod.detect_os,
    check_user_attrs: Callable = user.check_existing_user_attrs,
    deps_install: Callable = deps_debian.ensure_dependencies,
    docker_install: Callable = docker.ensure_docker,
    user_install: Callable = user.ensure_user,
    data_dir_install: Callable = _data_dir_mod.ensure_data_dir,
    fetch_repo: Callable = fetch.fetch_repo,
    setup_community_dir: Callable[[Path], None] = _default_setup_community_dir,
    backend_setup: Callable = backend.setup_backend,
    frontend_build: Callable = frontend.build_frontend,
    service_install: Callable = service.install_service,
    backup_timer_install: Callable = service.install_backup_timer,
    smoke_test: Callable = _smoke_mod.smoke_test,
    resolve_hostname: Callable = _post_install_mod._resolve_hostname,
    post_install_write: Callable = _post_install_mod.write,
    write_wrapper: Callable = _post_install_mod.write_wrapper,
    detect_existing: Callable = _install_mod.detect_existing_install,
    stdin_is_tty: Callable = sys.stdin.isatty,
    install_dir_exists: Callable[[Path], bool] = lambda p: p.exists(),
    remove_install_dir: Callable[[Path], None] = lambda p: shutil.rmtree(p, ignore_errors=True),
    stop_service: Callable[[], None] = lambda: _run_mod.run_required(
        ["systemctl", "stop", "slop.service"]
    ),
) -> int:
    """Orchestrate the Tier 2 install modules per ADR 0013 §3 and §4.

    All keyword-only arguments are injectable for unit testing; production
    callers use only positional args (the argparse Namespace from _cmd_install).
    """
    install_dir = args.install_dir
    data_dir = args.data_dir
    port = getattr(args, "port", _DEFAULT_PORT)
    version_ref = getattr(args, "version_ref", None)

    if not install_dir:
        print("error: --install-dir is required (provided by install.sh automatically).")
        return 1
    if not data_dir:
        print("error: --data-dir is required (provided by install.sh automatically).")
        return 1

    install_dir_path = Path(install_dir)
    data_dir_path = Path(data_dir)
    state_file_path = install_dir_path / state.STATE_FILE_NAME

    # ── ADR 0013 §4 / ADR 0015 §7 five-state machine (S1-S5 + S2a/S2b) ──
    det = detect_existing(
        state_file_path,
        install_dir_path,
        state_read=state_read,
        install_dir_exists=install_dir_exists,
    )

    _force_cleanup = False
    if det.state != "clean":
        if det.state in ("s2a", "s2b") and not args.force:
            # Idempotent no-op: already installed. Print info, exit successfully.
            print(det.message)
            return 0
        if not det.forceable or not args.force:
            print(det.message)
            return 1
        # --force path: defer install-dir removal until after all read-only
        # checks pass, so a pre-flight failure never leaves a half-removed state.
        _force_cleanup = True

    # S1 (clean) or --force path: fall through to install

    # ── Consent resolution ────────────────────────────────────────────────
    try:
        consent_mode = _resolve_consent_mode(args, stdin_is_tty)
    except RuntimeError as exc:
        print(str(exc))
        return 1

    # ── Pre-flight prereq checks (ADR 0013 §3 step 7 — read-only) ────────
    # For --force on a live install: stop the service first so the port check
    # finds 8080 free.  Soft-fail is intentional — service may not be running
    # (e.g. S3 interrupted install).
    if _force_cleanup:
        stop_service()
    findings = prereq_check(install_dir_path, port)
    failed = [f for f in findings if not f.ok]
    if failed:
        print("Pre-flight checks failed:")
        for f in findings:
            mark = "OK  " if f.ok else "FAIL"
            print(f"  [{mark}] {f.name}")
            if not f.ok:
                print(f"         Remediation: {f.remediation}")
        return 1

    # ── Distro detection (ADR 0013 §3 step 4) ────────────────────────────
    try:
        distro_info = detect_os()
    except _detect_mod.UnsupportedDistroError as exc:
        print(str(exc))
        return 1

    # ── Pre-flight user attribute check (ADR 0013 §5) ─────────────────────
    # Read-only getent check; must fire before any filesystem write so a
    # mismatch refusal never leaves a leftover install dir or state file.
    try:
        check_user_attrs()
    except user.InstallUserMismatchError as exc:
        print(str(exc))
        return 1

    # ── --force cleanup: remove old install dir after all read-only checks ──
    # Data dir is NOT touched (preserved across reinstalls per ADR 0015 §7).
    if _force_cleanup:
        remove_install_dir(install_dir_path)

    # ── Pre-write state file (ADR 0013 §2, phase=installing) ─────────────
    # write_state_file creates install_dir if absent — this is the §2 "first
    # filesystem write" point.  All steps above are read-only.
    now = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    pre_state = state.StateFile(
        schema_version=1,
        slop_version=_INSTALLER_VERSION,
        phase="installing",
        started_at=now,
        completed_at=None,
        install_dir=str(install_dir_path),
        data_dir=str(data_dir_path),
        install_user="slop",
        distro=distro_info.distro,
        distro_version=distro_info.version,
        port=port,
        smoke_test_passed=False,
    )
    state_write(pre_state, state_file_path)

    # ── Install pipeline (DEPENDENCIES.md §Install Ordering) ─────────────
    def _smoke_check(step_name: str) -> bool:
        """Return True (and print) if MS_SMOKE_STOP_AFTER requests a stop here."""
        if os.environ.get("MS_SMOKE_STOP_AFTER") == step_name:
            print(f"smoke-stop: {step_name}")
            return True
        return False

    try:
        print("[1/8] Installing system dependencies...", flush=True)
        deps_install(distro_info.distro)
        if _smoke_check("deps_install"):
            return 0
        print("[2/8] Installing Docker...", flush=True)
        docker_install(consent_mode)
        if _smoke_check("docker_install"):
            return 0
        print("[3/8] Creating install user...", flush=True)
        user_install()
        if _smoke_check("user_install"):
            return 0
        print("[4/8] Setting up data directory...", flush=True)
        data_dir_install(data_dir_path)
        if _smoke_check("data_dir_install"):
            return 0
        print("[5/8] Downloading SLOP...", flush=True)
        resolved_version = fetch_repo(
            install_dir_path,
            version_ref=version_ref,
            verify_tree=not getattr(args, "skip_tree_verify", False),
        )
        if _smoke_check("fetch_repo"):
            return 0
        # Pre-create catalog/community/ with slop ownership so the running
        # service can write custom app manifests without needing a post-install step.
        # The clone does not create this directory (git omits empty dirs).
        # slop user exists at this point — user_install ran at step [3/8].
        setup_community_dir(install_dir_path)
        # Steps [6/8] and [7/8] run in parallel — pip install and npm ci/build
        # have no shared filesystem state:
        #   backend_setup  → install_dir/.venv/  (pip install -r requirements.txt)
        #   frontend_build → install_dir/frontend/node_modules/ + backend/static/
        # Running them concurrently saves ~30s on a typical install.
        print("[6/8] Installing Python dependencies...", flush=True)
        print("[7/8] Building frontend... (steps 6 and 7 run in parallel)", flush=True)
        _run_parallel(
            lambda: backend_setup(install_dir_path),
            lambda: frontend_build(install_dir_path),
        )
        # Smoke-stop checks fire after BOTH parallel steps complete.
        # MS_SMOKE_STOP_AFTER=backend_setup or =frontend_build both stop here.
        if _smoke_check("backend_setup"):
            return 0
        if _smoke_check("frontend_build"):
            return 0
        print("[8/8] Installing and starting service...", flush=True)
        service_install(install_dir_path, data_dir_path)
        # #868 P3: scheduled-backup timer. Recoverability is advisory, not availability —
        # a timer-install failure must NOT abort an otherwise-good install (design §7/§9).
        try:
            backup_timer_install(install_dir_path, data_dir_path)
            print("      scheduled-backup timer enabled (ms-backup.timer, daily).", flush=True)
        except Exception as timer_exc:  # advisory step — warn, never abort the install
            print(
                f"      warning: scheduled-backup timer not installed "
                f"({type(timer_exc).__name__}: {timer_exc}). SLOP is healthy; run "
                "`ms-backup --all` manually or re-run install to retry scheduling.",
                flush=True,
            )
    except Exception as exc:
        print(
            f"Install failed in {type(exc).__name__}: {exc}\n"
            f"State file at {state_file_path} records this install as incomplete\n"
            "(phase: installing). Re-run with --force to clean and retry."
        )
        return 1

    # ── Post-write state file (ADR 0013 §2, phase=installed) ─────────────
    completed_at = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    post_state = state.StateFile(
        schema_version=1,
        slop_version=resolved_version or _INSTALLER_VERSION,
        phase="installed",
        started_at=pre_state.started_at,
        completed_at=completed_at,
        install_dir=str(install_dir_path),
        data_dir=str(data_dir_path),
        install_user="slop",
        distro=distro_info.distro,
        distro_version=distro_info.version,
        port=port,
        smoke_test_passed=False,  # Tier 3 smoke test updates this field
    )
    state_write(post_state, state_file_path)

    # ── Smoke test (ADR 0015 §5) ─────────────────────────────────────────────
    smoke_result = smoke_test(
        install_dir_path,
        data_dir=str(data_dir_path),
    )

    if not smoke_result.passed:
        # Smoke failure: no state-file mutation beyond the post-write above.
        # No POST_INSTALL.txt written. Failure to stderr, exit non-zero.
        print(
            f"\nSmoke test failed [{smoke_result.predicate} — "
            f"{smoke_result.failure_shape}]:\n"
            f"  {smoke_result.operator_message}\n\n"
            f"Diagnostic: {smoke_result.diagnostic_command}",
            file=sys.stderr,
        )
        return 1

    # ── Third state-file write: smoke_test_passed=true (ADR 0015 §5) ─────────
    smoke_state = dataclasses.replace(post_state, smoke_test_passed=True)
    state_write(smoke_state, state_file_path)

    # ── Write POST_INSTALL.txt (ADR 0015 §6) ─────────────────────────────────
    hostname = resolve_hostname()
    rendered = _post_install_mod.render(
        version=smoke_state.slop_version,
        hostname=hostname,
        port=port,
        install_dir=str(install_dir_path),
        data_dir=str(data_dir_path),
        completed_at=completed_at,
    )
    post_install_write(rendered, install_dir_path)
    write_wrapper(install_dir_path)

    # ── Stdout banner (ADR 0015 §6) ───────────────────────────────────────────
    banner_line = "=" * 50
    print(
        f"\n{banner_line}\n"
        f"Install complete. See {install_dir_path / 'POST_INSTALL.txt'}\n"
        f"{banner_line}\n\n" + rendered
    )
    return 0


def _cmd_install(args: argparse.Namespace) -> int:
    return run_install_pipeline(args)


def _cmd_uninstall(args: argparse.Namespace) -> int:
    return _uninstall_mod.run_uninstall(args)


def _cmd_purge(args: argparse.Namespace) -> int:
    return _uninstall_mod.run_purge(args)


def _cmd_clean(args: argparse.Namespace) -> int:
    return _uninstall_mod.run_clean(args)


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        result = _run_mod.run_required(["systemctl", "status", "slop.service"], timeout=10)
        return result.returncode
    except _run_mod.MissingBinaryError:
        print("not available")
        return 1
    except _run_mod.RunTimeoutError:
        print("timeout")
        return 1


def _add_path_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--install-dir",
        metavar="PATH",
        help="Directory to install slop code",
    )
    parser.add_argument(
        "--data-dir",
        metavar="PATH",
        help="Directory for slop runtime data",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="slop-installer",
        description="SLOP v5 installer",
    )
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.required = True

    # install
    p_install = sub.add_parser("install", help="Install slop on this host")
    _add_path_args(p_install)
    p_install.add_argument(
        "--install-docker",
        choices=["yes", "no"],
        help="Whether to install Docker if absent (required in pipe mode)",
    )
    p_install.add_argument(
        "--version-ref",
        default=None,
        metavar="REF",
        help="Git tag or ref to install (default: latest v5.x.y tag in the repo)",
    )
    p_install.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing install (data directory is preserved)",
    )
    p_install.add_argument(
        "--skip-tree-verify",
        action="store_true",
        help="Skip tree integrity verification after clone (not recommended)",
    )
    p_install.set_defaults(func=_cmd_install)

    # uninstall
    p_uninstall = sub.add_parser(
        "uninstall",
        help="Remove slop code, service, and user (preserves data directory)",
    )
    _add_path_args(p_uninstall)
    p_uninstall.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    p_uninstall.set_defaults(func=_cmd_uninstall)

    # purge
    p_purge = sub.add_parser(
        "purge",
        help="Remove everything: code, service, user, data, and managed containers",
    )
    _add_path_args(p_purge)
    p_purge.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    p_purge.set_defaults(func=_cmd_purge)

    # clean
    p_clean = sub.add_parser(
        "clean",
        help="Remove managed containers and their data; leave slop itself running",
    )
    _add_path_args(p_clean)
    p_clean.add_argument("--yes", action="store_true", help="Skip confirmation prompt")
    p_clean.set_defaults(func=_cmd_clean)

    # status
    p_status = sub.add_parser("status", help="Show current install status")
    _add_path_args(p_status)
    p_status.set_defaults(func=_cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
