"""installer/install.py - existing-install detection per ADR 0013 §4 + ADR 0015 §7.

detect_existing_install() is the single point of truth for the five-state
machine (S1-S5). Sub-task 3.2.c extends S2 to distinguish S2a (healthy) from
S2b (pipeline complete but smoke failed) per ADR 0015 §7.

The function is pure: reads files, does not write. Callers decide whether to
proceed based on ExistingInstallDetection.state and .forceable.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable

from installer.state import (
    StateFile,
    StateFileCorruptedError,
    StateFileNewerSchemaError,
    read_state_file,
)

_POST_INSTALL_NAME = "POST_INSTALL.txt"


@dataclass
class ExistingInstallDetection:
    """Result of detect_existing_install().

    state: one of "clean", "s2a", "s2b", "s3", "s4", "s5"
    message: operator-facing message; empty for "clean" (proceeds silently)
    forceable: True if --force can override; False for S4 (corrupted_state)
    existing: the parsed StateFile if present; None for clean/partial/corrupted
    """

    state: str
    message: str
    forceable: bool
    existing: StateFile | None


def detect_existing_install(
    state_file_path: Path,
    install_dir: Path,
    *,
    state_read: Callable = read_state_file,
    post_install_exists: Callable[[Path], bool] = lambda p: p.exists(),
    install_dir_exists: Callable[[Path], bool] = lambda p: p.exists(),
) -> ExistingInstallDetection:
    """Detect the state of any existing slop install on this host.

    Checks three signals in precedence order (S2 → S3 → S4 → S5 → S1/clean):
      - state_file_path: the .installer-state.json file
      - install_dir: the install directory
      - POST_INSTALL.txt: presence signals smoke_test_passed for S2a/S2b split

    Returns ExistingInstallDetection; does not write or raise on normal paths.
    Raises only on unexpected internal errors (not state-file parse failures —
    those are returned as S4).

    Per ADR 0013 §4 and ADR 0015 §7:
      S2a: phase=installed + (smoke_test_passed=True OR post_install present)
      S2b: phase=installed + smoke_test_passed=False + post_install absent
      S3:  phase=installing (interrupted install)
      S4:  state file present but corrupted (--force does NOT override)
      S5:  state file absent, install dir exists (v4.x or hand-rolled)
      S1/clean: no signals — proceed
    """
    existing: StateFile | None = None

    try:
        existing = state_read(state_file_path)
    except (StateFileCorruptedError, StateFileNewerSchemaError) as exc:
        return ExistingInstallDetection(
            state="s4",
            message=(
                f"The state file at {state_file_path} exists but is unreadable: {exc}\n"
                "This is unusual and may indicate filesystem corruption, an external\n"
                "editor, or a state file from a future installer version.\n"
                f"Remove {state_file_path} manually, then re-run with --force,\n"
                "accepting that state recovery is your responsibility."
            ),
            forceable=False,
            existing=None,
        )

    if existing is not None and existing.phase == "installed":
        # S2: installed — distinguish S2a from S2b per ADR 0015 §7
        smoke_passed = existing.smoke_test_passed
        post_install_path = install_dir / _POST_INSTALL_NAME
        post_install_present = post_install_exists(post_install_path)

        if post_install_present and not smoke_passed:
            # Contradiction: POST_INSTALL.txt present but smoke_test_passed=False.
            # Should not occur per write-coupling contract. Log conservative S2b.
            # (We do not print here; the caller surfaces the message.)
            _state_label = "s2b"
            _message = (
                f"slop {existing.slop_version} was installed at "
                f"{install_dir}\n"
                f"(installed {existing.completed_at}), but its smoke test did not "
                "pass.\n"
                "Note: POST_INSTALL.txt is present despite smoke_test_passed=false —\n"
                "this is a state contradiction. Treating as smoke-failed (S2b).\n"
                "The install pipeline completed but runtime readiness was not "
                "confirmed.\n"
                f"Re-run with --force to fully reinstall (this preserves "
                f"{existing.data_dir}),\n"
                "or check `journalctl -u slop.service` for the original "
                "failure.\n"
                "A standalone smoke-rerun subcommand is planned for v5.1."
            )
        elif smoke_passed or post_install_present:
            # S2a: healthy install (smoke passed, or file present with smoke True)
            _state_label = "s2a"
            _message = (
                f"slop {existing.slop_version} is already installed at\n"
                f"{install_dir} (installed {existing.completed_at}).\n"
                f"Re-run with --force to reinstall (this preserves "
                f"{existing.data_dir}).\n"
                "Or use `slop uninstall` to remove first."
            )
        else:
            # S2b: pipeline completed but smoke failed (smoke_test_passed=False,
            # POST_INSTALL.txt absent)
            _state_label = "s2b"
            _message = (
                f"slop {existing.slop_version} was installed at "
                f"{install_dir}\n"
                f"(installed {existing.completed_at}), but its smoke test did not "
                "pass.\n"
                "The install pipeline completed but runtime readiness was not "
                "confirmed.\n"
                f"Re-run with --force to fully reinstall (this preserves "
                f"{existing.data_dir}),\n"
                "or check `journalctl -u slop.service` for the original "
                "failure.\n"
                "A standalone smoke-rerun subcommand is planned for v5.1."
            )

        return ExistingInstallDetection(
            state=_state_label,
            message=_message,
            forceable=True,
            existing=existing,
        )

    if existing is not None and existing.phase == "installing":
        # S3: in-progress / interrupted
        return ExistingInstallDetection(
            state="s3",
            message=(
                "A previous slop install was interrupted\n"
                f"(started {existing.started_at}, never completed).\n"
                "Re-run with --force to clean and retry.\n"
                "Check `journalctl -u slop` for diagnostics from the "
                "previous attempt."
            ),
            forceable=True,
            existing=existing,
        )

    if existing is None and install_dir_exists(install_dir):
        # S5: partial (install dir present, no state file)
        return ExistingInstallDetection(
            state="s5",
            message=(
                f"{install_dir} exists but no slop v5 state file is present.\n"
                "This may be a v4.x install, a hand-rolled deployment, or an "
                "incomplete\n"
                "v5 install. Re-run with --force to remove and replace, or remove "
                "these\n"
                "manually if you want to preserve their contents."
            ),
            forceable=True,
            existing=None,
        )

    # S1: clean — no existing install signals
    return ExistingInstallDetection(
        state="clean",
        message="",
        forceable=True,
        existing=None,
    )
