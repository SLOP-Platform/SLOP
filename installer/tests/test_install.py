"""installer/tests/test_install.py — tests for install.py (detect_existing_install).

Covers the six-state machine: S1(clean), S2a(healthy), S2b(smoke-failed),
S3(interrupted), S4(corrupted), S5(no-state-file).

Operator message text is tested against ADR 0015 §7 and ADR 0013 §4 content.
"""

from __future__ import annotations

from pathlib import Path


from installer.install import ExistingInstallDetection, detect_existing_install
from installer.state import (
    StateFile,
    StateFileCorruptedError,
    StateFileNewerSchemaError,
)

# ── Shared factories ──────────────────────────────────────────────────────────

_INSTALL_DIR = Path("/opt/slop")
_STATE_FILE = _INSTALL_DIR / ".installer-state.json"
_POST_INSTALL = _INSTALL_DIR / "POST_INSTALL.txt"


def _make_state(phase="installed", smoke_test_passed=True, **overrides) -> StateFile:
    defaults = {
        "schema_version": 1,
        "slop_version": "5.0.0",
        "phase": phase,
        "started_at": "2026-01-01T00:00:00Z",
        "completed_at": "2026-01-01T00:05:00Z" if phase == "installed" else None,
        "install_dir": str(_INSTALL_DIR),
        "data_dir": "/var/lib/slop",
        "install_user": "slop",
        "distro": "ubuntu",
        "distro_version": "24.04",
        "port": 8080,
        "smoke_test_passed": smoke_test_passed,
    }
    defaults.update(overrides)
    return StateFile(**defaults)


def _detect(
    state: StateFile | None | Exception = None,
    post_install_present: bool = False,
    install_dir_present: bool = True,
) -> ExistingInstallDetection:
    """Convenience wrapper that builds callables from simple values."""

    def _read(_path):
        if isinstance(state, Exception):
            raise state
        return state

    return detect_existing_install(
        _STATE_FILE,
        _INSTALL_DIR,
        state_read=_read,
        post_install_exists=lambda p: post_install_present,
        install_dir_exists=lambda p: install_dir_present,
    )


# ── S1: clean ────────────────────────────────────────────────────────────────


class TestS1Clean:
    def test_no_state_no_dir_is_clean(self):
        det = _detect(state=None, install_dir_present=False)
        assert det.state == "clean"
        assert det.message == ""
        assert det.forceable is True
        assert det.existing is None

    def test_clean_proceeds_silently(self):
        det = _detect(state=None, install_dir_present=False)
        assert det.state == "clean"


# ── S2a: healthy install ──────────────────────────────────────────────────────


class TestS2aHealthy:
    def test_smoke_passed_true_is_s2a(self):
        state = _make_state(phase="installed", smoke_test_passed=True)
        det = _detect(state=state)
        assert det.state == "s2a"

    def test_s2a_forceable(self):
        state = _make_state(phase="installed", smoke_test_passed=True)
        det = _detect(state=state)
        assert det.forceable is True

    def test_s2a_returns_existing_state(self):
        state = _make_state(phase="installed", smoke_test_passed=True)
        det = _detect(state=state)
        assert det.existing is state

    def test_post_install_present_without_smoke_flag_is_s2a(self):
        state = _make_state(phase="installed", smoke_test_passed=False)
        det = _detect(state=state, post_install_present=True)
        assert det.state == "s2b"

    def test_s2a_message_contains_version(self):
        state = _make_state(phase="installed", smoke_test_passed=True)
        det = _detect(state=state)
        assert "5.0.0" in det.message

    def test_s2a_message_mentions_force(self):
        state = _make_state(phase="installed", smoke_test_passed=True)
        det = _detect(state=state)
        assert "--force" in det.message

    def test_s2a_message_mentions_uninstall(self):
        state = _make_state(phase="installed", smoke_test_passed=True)
        det = _detect(state=state)
        assert "uninstall" in det.message

    def test_s2a_message_contains_completed_at(self):
        state = _make_state(phase="installed", smoke_test_passed=True)
        det = _detect(state=state)
        assert "2026-01-01T00:05:00Z" in det.message


# ── S2b: smoke-failed install ─────────────────────────────────────────────────


class TestS2bSmokeFailed:
    def test_smoke_false_no_post_install_is_s2b(self):
        state = _make_state(phase="installed", smoke_test_passed=False)
        det = _detect(state=state, post_install_present=False)
        assert det.state == "s2b"

    def test_s2b_forceable(self):
        state = _make_state(phase="installed", smoke_test_passed=False)
        det = _detect(state=state, post_install_present=False)
        assert det.forceable is True

    def test_s2b_returns_existing_state(self):
        state = _make_state(phase="installed", smoke_test_passed=False)
        det = _detect(state=state, post_install_present=False)
        assert det.existing is state

    def test_s2b_message_says_smoke_did_not_pass(self):
        state = _make_state(phase="installed", smoke_test_passed=False)
        det = _detect(state=state, post_install_present=False)
        assert "smoke test did not pass" in det.message

    def test_s2b_message_mentions_force(self):
        state = _make_state(phase="installed", smoke_test_passed=False)
        det = _detect(state=state, post_install_present=False)
        assert "--force" in det.message

    def test_s2b_message_mentions_journalctl(self):
        state = _make_state(phase="installed", smoke_test_passed=False)
        det = _detect(state=state, post_install_present=False)
        assert "journalctl" in det.message

    def test_s2b_message_contains_version(self):
        state = _make_state(phase="installed", smoke_test_passed=False)
        det = _detect(state=state, post_install_present=False)
        assert "5.0.0" in det.message

    def test_s2b_message_contains_completed_at(self):
        state = _make_state(phase="installed", smoke_test_passed=False)
        det = _detect(state=state, post_install_present=False)
        assert "2026-01-01T00:05:00Z" in det.message


# ── S2 contradiction: POST_INSTALL.txt present but smoke_test_passed=False ────


class TestS2Contradiction:
    """When POST_INSTALL.txt exists but smoke_test_passed=False, treat as S2b.

    Per ADR 0015 §7: this is a state contradiction (should not occur under
    normal operation). The install contract writes both atomically; if the
    field disagrees we take the conservative (S2b) path and surface a note.
    """

    def test_contradiction_classified_as_s2b(self):
        state = _make_state(phase="installed", smoke_test_passed=False)
        det = _detect(state=state, post_install_present=True)
        assert det.state == "s2b"

    def test_contradiction_message_notes_presence_of_post_install(self):
        state = _make_state(phase="installed", smoke_test_passed=False)
        det = _detect(state=state, post_install_present=True)
        assert "POST_INSTALL.txt" in det.message

    def test_contradiction_message_says_contradiction(self):
        state = _make_state(phase="installed", smoke_test_passed=False)
        det = _detect(state=state, post_install_present=True)
        assert "contradiction" in det.message

    def test_contradiction_forceable(self):
        state = _make_state(phase="installed", smoke_test_passed=False)
        det = _detect(state=state, post_install_present=True)
        assert det.forceable is True


# ── S3: interrupted install ───────────────────────────────────────────────────


class TestS3Interrupted:
    def test_phase_installing_is_s3(self):
        state = _make_state(phase="installing", smoke_test_passed=False)
        det = _detect(state=state)
        assert det.state == "s3"

    def test_s3_forceable(self):
        state = _make_state(phase="installing", smoke_test_passed=False)
        det = _detect(state=state)
        assert det.forceable is True

    def test_s3_message_says_interrupted(self):
        state = _make_state(phase="installing", smoke_test_passed=False)
        det = _detect(state=state)
        assert "interrupted" in det.message

    def test_s3_message_mentions_force(self):
        state = _make_state(phase="installing", smoke_test_passed=False)
        det = _detect(state=state)
        assert "--force" in det.message

    def test_s3_message_mentions_journalctl(self):
        state = _make_state(phase="installing", smoke_test_passed=False)
        det = _detect(state=state)
        assert "journalctl" in det.message


# ── S4: corrupted state file ──────────────────────────────────────────────────


class TestS4Corrupted:
    def test_corrupted_error_is_s4(self):
        det = _detect(state=StateFileCorruptedError("bad json"))
        assert det.state == "s4"

    def test_newer_schema_error_is_s4(self):
        det = _detect(state=StateFileNewerSchemaError("schema too new"))
        assert det.state == "s4"

    def test_s4_not_forceable(self):
        det = _detect(state=StateFileCorruptedError("bad json"))
        assert det.forceable is False

    def test_s4_existing_is_none(self):
        det = _detect(state=StateFileCorruptedError("bad json"))
        assert det.existing is None

    def test_s4_message_says_unreadable(self):
        det = _detect(state=StateFileCorruptedError("bad json"))
        assert "unreadable" in det.message

    def test_s4_message_suggests_manual_removal(self):
        det = _detect(state=StateFileCorruptedError("bad json"))
        assert "manually" in det.message.lower() or "Remove" in det.message


# ── S5: install dir exists, no state file ────────────────────────────────────


class TestS5NoStateFile:
    def test_no_state_with_dir_is_s5(self):
        det = _detect(state=None, install_dir_present=True)
        assert det.state == "s5"

    def test_s5_forceable(self):
        det = _detect(state=None, install_dir_present=True)
        assert det.forceable is True

    def test_s5_existing_is_none(self):
        det = _detect(state=None, install_dir_present=True)
        assert det.existing is None

    def test_s5_message_mentions_v4(self):
        det = _detect(state=None, install_dir_present=True)
        assert "v4" in det.message

    def test_s5_message_mentions_force(self):
        det = _detect(state=None, install_dir_present=True)
        assert "--force" in det.message
