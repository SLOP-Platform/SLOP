"""installer/tests/test_state.py — unit tests for installer/state.py.

Covers the four scenarios named in V5_INSTALLER_PLAN.md Step 1.3.d:
  - write → read roundtrip (write lifecycle)
  - missing file returns None
  - malformed file raises StateFileCorruptedError
  - schema-validation rejects unknown fields / wrong types / version mismatch
"""

import json
from pathlib import Path

import pytest

from installer.state import (
    SUPPORTED_SCHEMA_VERSION,
    STATE_FILE_NAME,
    StateFile,
    StateFileCorruptedError,
    StateFileNewerSchemaError,
    read_state_file,
    write_state_file,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _valid_dict(**overrides) -> dict:
    """Return a valid raw-dict representation of a state file."""
    d = {
        "schema_version": 1,
        "slop_version": "5.0.0",
        "phase": "installed",
        "started_at": "2026-05-12T14:23:11Z",
        "completed_at": "2026-05-12T14:25:47Z",
        "install_dir": "/opt/slop",
        "data_dir": "/var/lib/slop",
        "install_user": "slop",
        "distro": "debian",
        "distro_version": "12",
        "port": 8080,
        "smoke_test_passed": True,
    }
    d.update(overrides)
    return d


def _valid_state(**overrides) -> StateFile:
    return StateFile(**dict(_valid_dict(**overrides).items()))


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


# ── TestWriteLifecycle ────────────────────────────────────────────────────────


class TestWriteLifecycle:
    """write_state_file → read_state_file roundtrip per ADR 0013 §2 Write lifecycle."""

    def test_pre_write_roundtrip(self, tmp_path):
        """Pre-write: phase=installing, completed_at=None, smoke_test_passed=False."""
        state_path = tmp_path / STATE_FILE_NAME
        pre = _valid_state(phase="installing", completed_at=None, smoke_test_passed=False)
        write_state_file(pre, state_path)

        result = read_state_file(state_path)

        assert result is not None
        assert result.phase == "installing"
        assert result.completed_at is None
        assert result.smoke_test_passed is False
        assert result.started_at == pre.started_at
        assert result.install_dir == pre.install_dir

    def test_post_write_roundtrip(self, tmp_path):
        """Post-write: phase=installed, completed_at set, smoke_test_passed=True."""
        state_path = tmp_path / STATE_FILE_NAME
        post = _valid_state()
        write_state_file(post, state_path)

        result = read_state_file(state_path)

        assert result is not None
        assert result.phase == "installed"
        assert result.completed_at == post.completed_at
        assert result.smoke_test_passed is True
        assert result.schema_version == 1
        assert result.slop_version == "5.0.0"

    def test_full_field_roundtrip(self, tmp_path):
        """All twelve fields survive a write→read cycle unchanged."""
        state_path = tmp_path / STATE_FILE_NAME
        original = _valid_state(
            port=9090,
            distro="ubuntu",
            distro_version="22.04",
            install_user="slop",
        )
        write_state_file(original, state_path)
        result = read_state_file(state_path)

        assert result == original

    def test_atomic_write_creates_parent(self, tmp_path):
        """write_state_file creates parent directories if absent."""
        state_path = tmp_path / "nested" / "dir" / STATE_FILE_NAME
        write_state_file(_valid_state(), state_path)
        assert state_path.exists()

    def test_written_file_is_valid_json(self, tmp_path):
        """Written file must be parseable JSON with a trailing newline."""
        state_path = tmp_path / STATE_FILE_NAME
        write_state_file(_valid_state(), state_path)
        raw = state_path.read_text(encoding="utf-8")
        assert raw.endswith("\n")
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)


# ── TestMissingFile ───────────────────────────────────────────────────────────


class TestMissingFile:
    def test_missing_file_returns_none(self, tmp_path):
        result = read_state_file(tmp_path / STATE_FILE_NAME)
        assert result is None

    def test_missing_file_at_nonexistent_dir(self, tmp_path):
        result = read_state_file(tmp_path / "no" / "such" / "dir" / STATE_FILE_NAME)
        assert result is None


# ── TestCorruption ────────────────────────────────────────────────────────────


class TestCorruption:
    def test_not_json_raises(self, tmp_path):
        p = tmp_path / STATE_FILE_NAME
        p.write_text("not json at all\n", encoding="utf-8")
        with pytest.raises(StateFileCorruptedError, match="not valid JSON"):
            read_state_file(p)

    def test_json_array_not_object_raises(self, tmp_path):
        p = tmp_path / STATE_FILE_NAME
        p.write_text("[1, 2, 3]\n", encoding="utf-8")
        with pytest.raises(StateFileCorruptedError, match="JSON object"):
            read_state_file(p)

    def test_empty_file_raises(self, tmp_path):
        p = tmp_path / STATE_FILE_NAME
        p.write_text("", encoding="utf-8")
        with pytest.raises(StateFileCorruptedError):
            read_state_file(p)

    def test_truncated_json_raises(self, tmp_path):
        p = tmp_path / STATE_FILE_NAME
        p.write_text('{"schema_version": 1, "phase":', encoding="utf-8")
        with pytest.raises(StateFileCorruptedError, match="not valid JSON"):
            read_state_file(p)


# ── TestVersionMismatch ───────────────────────────────────────────────────────


class TestVersionMismatch:
    def test_future_schema_version_raises(self, tmp_path):
        p = tmp_path / STATE_FILE_NAME
        _write_json(p, _valid_dict(schema_version=SUPPORTED_SCHEMA_VERSION + 1))
        with pytest.raises(StateFileNewerSchemaError) as exc_info:
            read_state_file(p)
        msg = str(exc_info.value)
        assert "newer installer" in msg
        assert str(SUPPORTED_SCHEMA_VERSION) in msg

    def test_current_version_accepted(self, tmp_path):
        p = tmp_path / STATE_FILE_NAME
        _write_json(p, _valid_dict(schema_version=SUPPORTED_SCHEMA_VERSION))
        result = read_state_file(p)
        assert result is not None
        assert result.schema_version == SUPPORTED_SCHEMA_VERSION

    def test_schema_version_not_integer_raises(self, tmp_path):
        p = tmp_path / STATE_FILE_NAME
        _write_json(p, _valid_dict(schema_version="1"))
        with pytest.raises(StateFileCorruptedError, match="schema_version"):
            read_state_file(p)

    def test_schema_version_boolean_rejected(self, tmp_path):
        # bool is a subclass of int; state.py must reject it explicitly.
        p = tmp_path / STATE_FILE_NAME
        _write_json(p, _valid_dict(schema_version=True))
        with pytest.raises(StateFileCorruptedError, match="schema_version"):
            read_state_file(p)


# ── TestSchemaValidation ──────────────────────────────────────────────────────


class TestSchemaValidation:
    def test_unknown_field_raises(self, tmp_path):
        p = tmp_path / STATE_FILE_NAME
        _write_json(p, _valid_dict(extra_field="unexpected"))
        with pytest.raises(StateFileCorruptedError, match="unknown field"):
            read_state_file(p)

    def test_multiple_unknown_fields_named_in_error(self, tmp_path):
        p = tmp_path / STATE_FILE_NAME
        _write_json(p, _valid_dict(alpha="a", beta="b"))
        with pytest.raises(StateFileCorruptedError) as exc_info:
            read_state_file(p)
        msg = str(exc_info.value)
        assert "alpha" in msg
        assert "beta" in msg

    def test_invalid_phase_raises(self, tmp_path):
        p = tmp_path / STATE_FILE_NAME
        _write_json(p, _valid_dict(phase="broken"))
        with pytest.raises(StateFileCorruptedError, match="phase"):
            read_state_file(p)

    def test_port_as_string_raises(self, tmp_path):
        p = tmp_path / STATE_FILE_NAME
        _write_json(p, _valid_dict(port="8080"))
        with pytest.raises(StateFileCorruptedError, match="port"):
            read_state_file(p)

    def test_port_as_boolean_raises(self, tmp_path):
        p = tmp_path / STATE_FILE_NAME
        _write_json(p, _valid_dict(port=True))
        with pytest.raises(StateFileCorruptedError, match="port"):
            read_state_file(p)

    def test_smoke_test_passed_as_string_raises(self, tmp_path):
        p = tmp_path / STATE_FILE_NAME
        _write_json(p, _valid_dict(smoke_test_passed="true"))
        with pytest.raises(StateFileCorruptedError, match="smoke_test_passed"):
            read_state_file(p)

    def test_completed_at_wrong_type_raises(self, tmp_path):
        p = tmp_path / STATE_FILE_NAME
        _write_json(p, _valid_dict(completed_at=12345))
        with pytest.raises(StateFileCorruptedError, match="completed_at"):
            read_state_file(p)

    def test_completed_at_null_is_accepted(self, tmp_path):
        p = tmp_path / STATE_FILE_NAME
        _write_json(p, _valid_dict(phase="installing", completed_at=None, smoke_test_passed=False))
        result = read_state_file(p)
        assert result is not None
        assert result.completed_at is None

    @pytest.mark.parametrize(
        "field",
        [
            "slop_version",
            "phase",
            "started_at",
            "install_dir",
            "data_dir",
            "install_user",
            "distro",
            "distro_version",
        ],
    )
    def test_missing_required_string_field_raises(self, tmp_path, field):
        d = _valid_dict()
        del d[field]
        p = tmp_path / STATE_FILE_NAME
        _write_json(p, d)
        with pytest.raises(StateFileCorruptedError):
            read_state_file(p)

    @pytest.mark.parametrize(
        "field",
        [
            "slop_version",
            "started_at",
            "install_dir",
            "data_dir",
            "install_user",
            "distro",
            "distro_version",
        ],
    )
    def test_non_string_for_string_field_raises(self, tmp_path, field):
        d = _valid_dict()
        d[field] = 42
        p = tmp_path / STATE_FILE_NAME
        _write_json(p, d)
        with pytest.raises(StateFileCorruptedError, match=field):
            read_state_file(p)
