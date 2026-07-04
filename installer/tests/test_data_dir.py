"""installer/tests/test_data_dir.py — unit tests for installer/data_dir.py.

All I/O is mocked via ensure_data_dir keyword-only injection.
No real subprocess calls, no real filesystem access.

ADR 0013 §1: /var/lib/slop owned slop:slop, mode 0750.

Coverage:
  TestEnsureDataDirMkdir        — mkdir called; creation error propagates; failure halts
  TestEnsureDataDirChown        — chown called with correct spec; error raises
  TestEnsureDataDirChmod        — chmod called with 0o750; error raises
  TestEnsureDataDirOrdering     — mkdir → chown → chmod sequence enforced
  TestEnsureDataDirIdempotency  — all ops run on repeated calls (exist_ok semantics)
  TestEnsureDataDirBoundaryProbe — MissingBinaryError from missing chown/chmod binary
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from installer._run import MissingBinaryError
from installer.data_dir import (
    DataDirChmodError,
    DataDirChownError,
    DataDirCreationError,
    _run_chown,
    _run_chmod,
    ensure_data_dir,
)

_DATA_DIR = "/var/lib/slop"


def _noop(*args, **kwargs):
    pass


def _passing_kwargs(**overrides) -> dict:
    """Return ensure_data_dir kwargs for a successful call path."""
    base = {
        "make_dir": _noop,
        "run_chown": _noop,
        "run_chmod": _noop,
    }
    base.update(overrides)
    return base


# ── TestEnsureDataDirMkdir ────────────────────────────────────────────────────


class TestEnsureDataDirMkdir:
    def test_mkdir_called_with_correct_path(self):
        calls = []
        ensure_data_dir(
            _DATA_DIR,
            **_passing_kwargs(make_dir=lambda p: calls.append(p)),
        )
        assert calls == [Path(_DATA_DIR)]

    def test_mkdir_creation_error_propagates(self):
        def fail(p):
            raise DataDirCreationError("cannot create")

        with pytest.raises(DataDirCreationError):
            ensure_data_dir(_DATA_DIR, **_passing_kwargs(make_dir=fail))

    def test_mkdir_failure_skips_chown(self):
        chown_calls = []

        def fail(p):
            raise DataDirCreationError("cannot create")

        with pytest.raises(DataDirCreationError):
            ensure_data_dir(
                _DATA_DIR,
                make_dir=fail,
                run_chown=lambda u, g, p: chown_calls.append((u, g, p)),
                run_chmod=_noop,
            )
        assert chown_calls == []

    def test_mkdir_failure_skips_chmod(self):
        chmod_calls = []

        def fail(p):
            raise DataDirCreationError("cannot create")

        with pytest.raises(DataDirCreationError):
            ensure_data_dir(
                _DATA_DIR,
                make_dir=fail,
                run_chown=_noop,
                run_chmod=lambda m, p: chmod_calls.append((m, p)),
            )
        assert chmod_calls == []


# ── TestEnsureDataDirChown ────────────────────────────────────────────────────


class TestEnsureDataDirChown:
    def test_chown_called_with_default_user_group(self):
        calls = []
        ensure_data_dir(
            _DATA_DIR,
            **_passing_kwargs(run_chown=lambda u, g, p: calls.append((u, g))),
        )
        assert calls == [("slop", "slop")]

    def test_chown_called_with_correct_path(self):
        calls = []
        ensure_data_dir(
            _DATA_DIR,
            **_passing_kwargs(run_chown=lambda u, g, p: calls.append(p)),
        )
        assert calls == [Path(_DATA_DIR)]

    def test_custom_user_group_forwarded(self):
        calls = []
        ensure_data_dir(
            _DATA_DIR,
            user="svc",
            group="svc",
            **_passing_kwargs(run_chown=lambda u, g, p: calls.append((u, g))),
        )
        assert calls == [("svc", "svc")]

    def test_chown_error_propagates(self):
        def fail_chown(u, g, p):
            raise DataDirChownError(f"chown {u}:{g} failed")

        with pytest.raises(DataDirChownError):
            ensure_data_dir(_DATA_DIR, **_passing_kwargs(run_chown=fail_chown))


# ── TestEnsureDataDirChmod ────────────────────────────────────────────────────


class TestEnsureDataDirChmod:
    def test_chmod_called_with_default_mode_0o750(self):
        calls = []
        ensure_data_dir(
            _DATA_DIR,
            **_passing_kwargs(run_chmod=lambda m, p: calls.append(m)),
        )
        assert calls == [0o750]

    def test_chmod_called_with_correct_path(self):
        calls = []
        ensure_data_dir(
            _DATA_DIR,
            **_passing_kwargs(run_chmod=lambda m, p: calls.append(p)),
        )
        assert calls == [Path(_DATA_DIR)]

    def test_chmod_error_propagates(self):
        def fail_chmod(m, p):
            raise DataDirChmodError("chmod failed")

        with pytest.raises(DataDirChmodError):
            ensure_data_dir(_DATA_DIR, **_passing_kwargs(run_chmod=fail_chmod))


# ── TestEnsureDataDirOrdering ─────────────────────────────────────────────────


class TestEnsureDataDirOrdering:
    def test_mkdir_before_chown_before_chmod(self):
        order = []
        ensure_data_dir(
            _DATA_DIR,
            make_dir=lambda p: order.append("mkdir"),
            run_chown=lambda u, g, p: order.append("chown"),
            run_chmod=lambda m, p: order.append("chmod"),
        )
        assert order == ["mkdir", "chown", "chmod"]


# ── TestEnsureDataDirIdempotency ──────────────────────────────────────────────


class TestEnsureDataDirIdempotency:
    def test_all_ops_called_on_first_call(self):
        counts = {"mkdir": 0, "chown": 0, "chmod": 0}

        def inc(key):
            counts[key] += 1

        ensure_data_dir(
            _DATA_DIR,
            make_dir=lambda p: inc("mkdir"),
            run_chown=lambda u, g, p: inc("chown"),
            run_chmod=lambda m, p: inc("chmod"),
        )
        assert counts == {"mkdir": 1, "chown": 1, "chmod": 1}

    def test_all_ops_called_on_second_call(self):
        counts = {"mkdir": 0, "chown": 0, "chmod": 0}

        def inc(key):
            counts[key] += 1

        kwargs = {
            "make_dir": lambda p: inc("mkdir"),
            "run_chown": lambda u, g, p: inc("chown"),
            "run_chmod": lambda m, p: inc("chmod"),
        }
        ensure_data_dir(_DATA_DIR, **kwargs)
        ensure_data_dir(_DATA_DIR, **kwargs)
        assert counts == {"mkdir": 2, "chown": 2, "chmod": 2}


# ── TestEnsureDataDirBoundaryProbe ────────────────────────────────────────────


class TestEnsureDataDirBoundaryProbe:
    """Rule 5.27: every subprocess call must have a paired boundary test.

    _run_chown and _run_chmod both invoke run_required; a missing binary
    must surface as MissingBinaryError, not FileNotFoundError or crash.
    Non-zero returncode must raise the domain-specific error, not be silently
    swallowed.
    """

    def test_run_chown_raises_on_chown_absent(self):
        with patch(
            "installer._run.subprocess.run",
            side_effect=FileNotFoundError("chown"),
        ):
            with pytest.raises(MissingBinaryError, match="chown"):
                _run_chown("slop", "slop", Path(_DATA_DIR))

    def test_run_chmod_raises_on_chmod_absent(self):
        with patch(
            "installer._run.subprocess.run",
            side_effect=FileNotFoundError("chmod"),
        ):
            with pytest.raises(MissingBinaryError, match="chmod"):
                _run_chmod(0o750, Path(_DATA_DIR))

    def test_run_chown_nonzero_raises_chown_error(self):
        import types

        with patch(
            "installer._run.subprocess.run",
            return_value=types.SimpleNamespace(
                returncode=1, stdout="", stderr="operation not permitted"
            ),
        ):
            with pytest.raises(DataDirChownError):
                _run_chown("slop", "slop", Path(_DATA_DIR))

    def test_run_chmod_nonzero_raises_chmod_error(self):
        import types

        with patch(
            "installer._run.subprocess.run",
            return_value=types.SimpleNamespace(
                returncode=1, stdout="", stderr="operation not permitted"
            ),
        ):
            with pytest.raises(DataDirChmodError):
                _run_chmod(0o750, Path(_DATA_DIR))
