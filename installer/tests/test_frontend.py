"""installer/tests/test_frontend.py — unit tests for installer/frontend.py.

All filesystem and subprocess I/O is mocked via build_frontend keyword-only
injection.  No real npm invocations, no real filesystem access.

Coverage:
  TestBuildFrontendPackageJson  — missing package.json raises; path checked correctly
  TestBuildFrontendIdempotency  — existing build artifacts short-circuit both npm steps
  TestBuildFrontendNpmCi        — npm ci called with correct dir; failure propagation
  TestBuildFrontendNpmBuild     — npm run build called after ci; failure propagation
  TestBuildFrontendOrdering     — full call sequence and failure-halts-sequence
"""

from __future__ import annotations

from pathlib import Path

import pytest

from installer.frontend import (
    NpmBuildError,
    NpmCiError,
    PackageJsonNotFoundError,
    _run_npm_ci,
    _run_npm_build,
    build_frontend,
)


# ── Shared helpers ────────────────────────────────────────────────────────────


def _noop(*_args, **_kwargs):
    pass


def _passing_kwargs(
    pkg_json_present: bool = True,
    build_present: bool = False,
) -> dict:
    """Return build_frontend injectable kwargs for a default-success scenario."""
    return {
        "package_json_exists": lambda frontend_dir: pkg_json_present,
        "build_exists": lambda static_dir: build_present,
        "run_npm_ci": _noop,
        "run_npm_build": _noop,
    }


# ── TestBuildFrontendPackageJson ──────────────────────────────────────────────


class TestBuildFrontendPackageJson:
    def test_missing_package_json_raises(self):
        kwargs = _passing_kwargs(pkg_json_present=False)
        with pytest.raises(PackageJsonNotFoundError):
            build_frontend("/some/dir", **kwargs)

    def test_missing_package_json_message_names_file(self):
        kwargs = _passing_kwargs(pkg_json_present=False)
        with pytest.raises(PackageJsonNotFoundError, match=r"package.json"):
            build_frontend("/some/dir", **kwargs)

    def test_missing_package_json_skips_npm_ci(self):
        npm_ci_calls = []
        kwargs = _passing_kwargs(pkg_json_present=False)
        kwargs["run_npm_ci"] = lambda d: npm_ci_calls.append(d)
        with pytest.raises(PackageJsonNotFoundError):
            build_frontend("/some/dir", **kwargs)
        assert npm_ci_calls == []

    def test_package_json_check_uses_frontend_dir(self):
        dirs_checked = []
        kwargs = _passing_kwargs()
        kwargs["package_json_exists"] = lambda d: dirs_checked.append(d) or True
        build_frontend("/some/dir", **kwargs)
        assert dirs_checked == [Path("/some/dir/frontend")]


# ── TestBuildFrontendIdempotency ──────────────────────────────────────────────


class TestBuildFrontendIdempotency:
    def test_existing_build_skips_npm_ci(self):
        npm_ci_calls = []
        kwargs = _passing_kwargs(build_present=True)
        kwargs["run_npm_ci"] = lambda d: npm_ci_calls.append(d)
        build_frontend("/some/dir", **kwargs)
        assert npm_ci_calls == []

    def test_existing_build_skips_npm_build(self):
        npm_build_calls = []
        kwargs = _passing_kwargs(build_present=True)
        kwargs["run_npm_build"] = lambda d: npm_build_calls.append(d)
        build_frontend("/some/dir", **kwargs)
        assert npm_build_calls == []

    def test_existing_build_returns_without_error(self):
        kwargs = _passing_kwargs(build_present=True)
        build_frontend("/some/dir", **kwargs)  # must not raise

    def test_build_absent_proceeds_to_npm_ci(self):
        npm_ci_calls = []
        kwargs = _passing_kwargs(build_present=False)
        kwargs["run_npm_ci"] = lambda d: npm_ci_calls.append(d)
        build_frontend("/some/dir", **kwargs)
        assert len(npm_ci_calls) == 1

    def test_build_exists_check_uses_static_dir(self):
        dirs_checked = []
        kwargs = _passing_kwargs()
        kwargs["build_exists"] = lambda d: dirs_checked.append(d) or False
        build_frontend("/some/dir", **kwargs)
        assert dirs_checked == [Path("/some/dir/backend/static")]

    def test_missing_package_json_raises_even_if_build_exists(self):
        kwargs = _passing_kwargs(pkg_json_present=False, build_present=True)
        with pytest.raises(PackageJsonNotFoundError):
            build_frontend("/some/dir", **kwargs)


# ── TestBuildFrontendNpmCi ────────────────────────────────────────────────────


class TestBuildFrontendNpmCi:
    def test_npm_ci_called(self):
        calls = []
        kwargs = _passing_kwargs()
        kwargs["run_npm_ci"] = lambda d: calls.append(d)
        build_frontend("/some/dir", **kwargs)
        assert len(calls) == 1

    def test_npm_ci_receives_frontend_dir(self):
        dirs = []
        kwargs = _passing_kwargs()
        kwargs["run_npm_ci"] = lambda d: dirs.append(d)
        build_frontend("/some/dir", **kwargs)
        assert dirs == [Path("/some/dir/frontend")]

    def test_npm_ci_failure_raises(self):
        def fail_ci(d):
            raise NpmCiError("npm ci exit 1")

        kwargs = _passing_kwargs()
        kwargs["run_npm_ci"] = fail_ci
        with pytest.raises(NpmCiError):
            build_frontend("/some/dir", **kwargs)

    def test_npm_ci_failure_skips_npm_build(self):
        npm_build_calls = []

        def fail_ci(d):
            raise NpmCiError("failed")

        kwargs = _passing_kwargs()
        kwargs["run_npm_ci"] = fail_ci
        kwargs["run_npm_build"] = lambda d: npm_build_calls.append(d)
        with pytest.raises(NpmCiError):
            build_frontend("/some/dir", **kwargs)
        assert npm_build_calls == []


# ── TestBuildFrontendNpmBuild ─────────────────────────────────────────────────


class TestBuildFrontendNpmBuild:
    def test_npm_build_called_after_ci(self):
        calls = []
        kwargs = _passing_kwargs()
        kwargs["run_npm_build"] = lambda d: calls.append(d)
        build_frontend("/some/dir", **kwargs)
        assert len(calls) == 1

    def test_npm_build_receives_frontend_dir(self):
        dirs = []
        kwargs = _passing_kwargs()
        kwargs["run_npm_build"] = lambda d: dirs.append(d)
        build_frontend("/some/dir", **kwargs)
        assert dirs == [Path("/some/dir/frontend")]

    def test_npm_build_failure_raises(self):
        def fail_build(d):
            raise NpmBuildError("npm run build exit 1")

        kwargs = _passing_kwargs()
        kwargs["run_npm_build"] = fail_build
        with pytest.raises(NpmBuildError):
            build_frontend("/some/dir", **kwargs)


# ── TestBuildFrontendOrdering ─────────────────────────────────────────────────


class TestBuildFrontendOrdering:
    def test_npm_ci_before_npm_build(self):
        call_order = []
        kwargs = _passing_kwargs()
        kwargs["run_npm_ci"] = lambda d: call_order.append("ci")
        kwargs["run_npm_build"] = lambda d: call_order.append("build")
        build_frontend("/some/dir", **kwargs)
        assert call_order == ["ci", "build"]

    def test_npm_build_not_called_on_ci_failure(self):
        call_order = []

        def fail_ci(d):
            call_order.append("ci")
            raise NpmCiError("failed")

        kwargs = _passing_kwargs()
        kwargs["run_npm_ci"] = fail_ci
        kwargs["run_npm_build"] = lambda d: call_order.append("build")
        with pytest.raises(NpmCiError):
            build_frontend("/some/dir", **kwargs)
        assert "build" not in call_order


# ── TestNpmCiProbe ────────────────────────────────────────────────────────────


class TestNpmCiProbe:
    """Boundary tests for _run_npm_ci failure modes (F4 + F13)."""

    def test_raises_on_missing_frontend_dir(self, tmp_path):
        with pytest.raises(NpmCiError, match="does not exist"):
            _run_npm_ci(tmp_path / "nonexistent")

    def test_raises_on_npm_absent(self, tmp_path):
        from unittest.mock import patch

        frontend_dir = tmp_path / "frontend"
        frontend_dir.mkdir()
        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("npm")):
            with pytest.raises(NpmCiError, match="npm is not on PATH"):
                _run_npm_ci(frontend_dir)

    def test_raises_on_nonzero_returncode(self, tmp_path):
        from unittest.mock import patch, MagicMock

        frontend_dir = tmp_path / "frontend"
        frontend_dir.mkdir()
        fake = MagicMock(returncode=1, stdout="", stderr="ERESOLVE")
        with patch("installer._run.subprocess.run", return_value=fake):
            with pytest.raises(NpmCiError, match="npm ci failed"):
                _run_npm_ci(frontend_dir)

    def test_success_does_not_raise(self, tmp_path):
        from unittest.mock import patch, MagicMock

        frontend_dir = tmp_path / "frontend"
        frontend_dir.mkdir()
        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch("installer._run.subprocess.run", return_value=fake):
            _run_npm_ci(frontend_dir)  # must not raise


# ── TestNpmBuildProbe ─────────────────────────────────────────────────────────


class TestNpmBuildProbe:
    """Boundary tests for _run_npm_build failure modes (F4 + F13)."""

    def test_raises_on_missing_frontend_dir(self, tmp_path):
        with pytest.raises(NpmBuildError, match="does not exist"):
            _run_npm_build(tmp_path / "nonexistent")

    def test_raises_on_npm_absent(self, tmp_path):
        from unittest.mock import patch

        frontend_dir = tmp_path / "frontend"
        frontend_dir.mkdir()
        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("npm")):
            with pytest.raises(NpmBuildError, match="npm is not on PATH"):
                _run_npm_build(frontend_dir)

    def test_raises_on_nonzero_returncode(self, tmp_path):
        from unittest.mock import patch, MagicMock

        frontend_dir = tmp_path / "frontend"
        frontend_dir.mkdir()
        fake = MagicMock(returncode=1, stdout="", stderr="Build failed")
        with patch("installer._run.subprocess.run", return_value=fake):
            with pytest.raises(NpmBuildError, match="npm run build failed"):
                _run_npm_build(frontend_dir)

    def test_success_does_not_raise(self, tmp_path):
        from unittest.mock import patch, MagicMock

        frontend_dir = tmp_path / "frontend"
        frontend_dir.mkdir()
        fake = MagicMock(returncode=0, stdout="", stderr="")
        with patch("installer._run.subprocess.run", return_value=fake):
            _run_npm_build(frontend_dir)  # must not raise
