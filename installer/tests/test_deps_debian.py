"""installer/tests/test_deps_debian.py — unit tests for installer/deps_debian.py.

All apt/subprocess I/O is mocked via ensure_dependencies keyword-only injection.
No real dpkg calls, no real apt invocations, no real network access.

Coverage per DEPENDENCIES.md Maintenance Contract rule 1 (present-and-OK,
present-but-below-floor, absent per dependency entry):

  TestParseNodeVersion       — version-string parsing edge cases
  TestEnsureDependenciesCurl — curl: present/absent
  TestEnsureDependenciesNetcat — netcat-openbsd: present/absent
  TestEnsureDependenciesNodejs — nodejs: present-ok, present-below-floor, absent
  TestEnsureDependenciesOrdering — apt-get update and NodeSource ordering rules
  TestEnsureDependenciesErrors — error propagation from injected helpers
  TestDependenciesConstant   — DEPENDENCIES constant shape for INV-D2
"""

from __future__ import annotations


import pytest

from installer._run import MissingBinaryError
from installer.deps_debian import (
    DEPENDENCIES,
    AptLockError,
    AptUpdateNetworkError,
    DependencyVersionUnparseableError,
    NodeSourceSetupError,
    PackageNotFoundError,
    _is_pkg_installed,
    _parse_node_version,
    _run_apt_install,
    _run_apt_update,
    _run_nodesource_setup,
    ensure_dependencies,
)

# ── Shared test fixtures ──────────────────────────────────────────────────────

_ALL_PRESENT = {"curl": True, "netcat-openbsd": True}
_ALL_ABSENT = {"curl": False, "netcat-openbsd": False}


def _pkg_map(present: dict):
    """Return an is_pkg_installed callable using a pkg→bool dict."""
    return lambda pkg: present.get(pkg, False)


def _no_op(*_args, **_kwargs):
    pass


def _noop_apt_update():
    pass


def _noop_nodesource(distro):
    pass


def _noop_apt_install(pkgs):
    pass


def _passing_kwargs(
    pkgs: dict | None = None,
    node_version: str | None = "v22.5.0",
) -> dict:
    """Return ensure_dependencies kwargs where everything passes (all present, node ok)."""
    if pkgs is None:
        pkgs = {"curl": True, "netcat-openbsd": True}
    return {
        "is_pkg_installed": _pkg_map(pkgs),
        "get_node_version_str": lambda: node_version,
        "run_apt_update": _noop_apt_update,
        "run_nodesource_setup": _noop_nodesource,
        "run_apt_install": _noop_apt_install,
    }


# ── TestParseNodeVersion ──────────────────────────────────────────────────────


class TestParseNodeVersion:
    def test_at_floor(self):
        assert _parse_node_version("v20.19.0") == (20, 19)

    def test_above_floor(self):
        assert _parse_node_version("v22.5.0") == (22, 5)

    def test_below_floor(self):
        assert _parse_node_version("v18.0.0") == (18, 0)

    def test_no_v_prefix(self):
        assert _parse_node_version("20.19.0") == (20, 19)

    def test_unparseable_raises(self):
        with pytest.raises(DependencyVersionUnparseableError):
            _parse_node_version("not-a-version")

    def test_empty_raises(self):
        with pytest.raises(DependencyVersionUnparseableError):
            _parse_node_version("")


# ── TestEnsureDependenciesCurl ────────────────────────────────────────────────


class TestEnsureDependenciesCurl:
    def test_present_and_ok_skipped(self):
        installed = []
        kwargs = _passing_kwargs(pkgs={"curl": True, "netcat-openbsd": True})
        kwargs["run_apt_install"] = lambda pkgs: installed.extend(pkgs)
        result = ensure_dependencies(**kwargs)
        assert "curl" not in installed
        assert "curl" not in result

    def test_absent_added_to_install(self):
        installed = []
        kwargs = _passing_kwargs(pkgs={"curl": False, "netcat-openbsd": True})
        kwargs["run_apt_install"] = lambda pkgs: installed.extend(pkgs)
        result = ensure_dependencies(**kwargs)
        assert "curl" in installed
        assert "curl" in result

    def test_absent_triggers_apt_update(self):
        updates = []
        kwargs = _passing_kwargs(pkgs={"curl": False, "netcat-openbsd": True})
        kwargs["run_apt_update"] = lambda: updates.append(1)
        ensure_dependencies(**kwargs)
        assert len(updates) == 1


# ── TestEnsureDependenciesNetcat ──────────────────────────────────────────────


class TestEnsureDependenciesNetcat:
    def test_present_and_ok_skipped(self):
        installed = []
        kwargs = _passing_kwargs(pkgs={"curl": True, "netcat-openbsd": True})
        kwargs["run_apt_install"] = lambda pkgs: installed.extend(pkgs)
        result = ensure_dependencies(**kwargs)
        assert "netcat-openbsd" not in installed
        assert "netcat-openbsd" not in result

    def test_absent_added_to_install(self):
        installed = []
        kwargs = _passing_kwargs(pkgs={"curl": True, "netcat-openbsd": False})
        kwargs["run_apt_install"] = lambda pkgs: installed.extend(pkgs)
        result = ensure_dependencies(**kwargs)
        assert "netcat-openbsd" in installed
        assert "netcat-openbsd" in result


# ── TestEnsureDependenciesNodejs ──────────────────────────────────────────────


class TestEnsureDependenciesNodejs:
    def test_present_at_floor_skipped(self):
        installed = []
        nodesource_calls = []
        kwargs = _passing_kwargs(node_version="v20.19.0")
        kwargs["run_apt_install"] = lambda pkgs: installed.extend(pkgs)
        kwargs["run_nodesource_setup"] = lambda d: nodesource_calls.append(d)
        result = ensure_dependencies(**kwargs)
        assert "nodejs" not in installed
        assert len(nodesource_calls) == 0
        assert result == []

    def test_present_above_floor_skipped(self):
        installed = []
        kwargs = _passing_kwargs(node_version="v22.5.0")
        kwargs["run_apt_install"] = lambda pkgs: installed.extend(pkgs)
        ensure_dependencies(**kwargs)
        assert "nodejs" not in installed

    def test_present_below_floor_triggers_nodesource(self):
        nodesource_calls = []
        installed = []
        kwargs = _passing_kwargs(node_version="v18.0.0")
        kwargs["run_nodesource_setup"] = lambda d: nodesource_calls.append(d)
        kwargs["run_apt_install"] = lambda pkgs: installed.extend(pkgs)
        result = ensure_dependencies(**kwargs)
        assert len(nodesource_calls) == 1
        assert "nodejs" in installed
        assert "nodejs" in result

    def test_absent_triggers_nodesource(self):
        nodesource_calls = []
        installed = []
        kwargs = _passing_kwargs(node_version=None)
        kwargs["run_nodesource_setup"] = lambda d: nodesource_calls.append(d)
        kwargs["run_apt_install"] = lambda pkgs: installed.extend(pkgs)
        result = ensure_dependencies(**kwargs)
        assert len(nodesource_calls) == 1
        assert "nodejs" in installed
        assert "nodejs" in result


# ── TestGetNodeVersionStrProbe ────────────────────────────────────────────────


class TestGetNodeVersionStrProbe:
    """_get_node_version_str returns None when the node binary is absent."""

    def test_returns_none_on_file_not_found(self):
        from unittest.mock import patch
        from installer.deps_debian import _get_node_version_str

        # Patch at the subprocess boundary (installer._run) — deps_debian no
        # longer imports subprocess directly after run_required migration (F7).
        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("node")):
            assert _get_node_version_str() is None


# ── TestEnsureDependenciesOrdering ────────────────────────────────────────────


class TestEnsureDependenciesOrdering:
    def test_all_present_returns_empty(self):
        apt_update_calls = []
        apt_install_calls = []
        kwargs = _passing_kwargs()
        kwargs["run_apt_update"] = lambda: apt_update_calls.append(1)
        kwargs["run_apt_install"] = lambda p: apt_install_calls.append(p)
        result = ensure_dependencies(**kwargs)
        assert result == []
        assert len(apt_update_calls) == 0
        assert len(apt_install_calls) == 0

    def test_nodesource_called_before_apt_install(self):
        call_order = []
        kwargs = _passing_kwargs(node_version=None)
        kwargs["run_nodesource_setup"] = lambda d: call_order.append("nodesource")
        kwargs["run_apt_install"] = lambda p: call_order.append("apt_install")
        ensure_dependencies(**kwargs)
        assert call_order.index("nodesource") < call_order.index("apt_install")

    def test_apt_update_before_nodesource(self):
        # curl=False + nodejs=None: curl pre-install → nodesource → apt_install(rest).
        # Use tuple-tracking to distinguish the two apt_install calls (F2 fix).
        ops = []
        kwargs = _passing_kwargs(pkgs={"curl": False, "netcat-openbsd": True}, node_version=None)
        kwargs["run_apt_update"] = lambda: ops.append("apt_update")
        kwargs["run_nodesource_setup"] = lambda d: ops.append("nodesource")
        kwargs["run_apt_install"] = lambda p: ops.append(("apt_install", tuple(p)))
        ensure_dependencies(**kwargs)
        apt_update_idx = ops.index("apt_update")
        nodesource_idx = ops.index("nodesource")
        curl_install_idx = next(
            i for i, op in enumerate(ops) if isinstance(op, tuple) and "curl" in op[1]
        )
        assert apt_update_idx < curl_install_idx
        assert curl_install_idx < nodesource_idx

    def test_only_nodejs_missing_skips_apt_update(self):
        # NodeSource's setup_22.x runs its own apt-get update internally;
        # the installer skips a standalone apt-get update when only nodejs needs installing.
        apt_update_calls = []
        kwargs = _passing_kwargs(pkgs={"curl": True, "netcat-openbsd": True}, node_version=None)
        kwargs["run_apt_update"] = lambda: apt_update_calls.append(1)
        ensure_dependencies(**kwargs)
        assert len(apt_update_calls) == 0

    def test_curl_preinstalled_separately_when_nodesource_needed(self):
        # F2 fix: when curl and nodejs are both missing, curl is installed
        # in a separate apt-install call BEFORE NodeSource, then remaining
        # packages (netcat-openbsd, nodejs) are installed in a second call.
        apt_install_calls = []
        kwargs = _passing_kwargs(pkgs={"curl": False, "netcat-openbsd": False}, node_version=None)
        kwargs["run_apt_install"] = lambda p: apt_install_calls.append(list(p))
        ensure_dependencies(**kwargs)
        assert len(apt_install_calls) == 2
        assert apt_install_calls[0] == ["curl"]
        assert "netcat-openbsd" in apt_install_calls[1]
        assert "nodejs" in apt_install_calls[1]
        assert "curl" not in apt_install_calls[1]

    def test_curl_installed_before_nodesource_when_both_missing(self):
        # F2 fix: verify the call ordering — curl apt-install must precede
        # nodesource setup, which must precede the nodejs apt-install.
        order = []
        kwargs = _passing_kwargs(pkgs={"curl": False, "netcat-openbsd": True}, node_version=None)
        kwargs["run_apt_install"] = lambda pkgs: order.append(("apt_install", tuple(pkgs)))
        kwargs["run_nodesource_setup"] = lambda d: order.append(("nodesource",))
        ensure_dependencies(**kwargs)
        curl_idx = next(
            i for i, (op, *args) in enumerate(order) if op == "apt_install" and "curl" in args[0]
        )
        nodesource_idx = next(i for i, (op, *_) in enumerate(order) if op == "nodesource")
        assert curl_idx < nodesource_idx

    def test_distro_passed_to_nodesource(self):
        distros_seen = []
        kwargs = _passing_kwargs(node_version=None)
        kwargs["run_nodesource_setup"] = lambda d: distros_seen.append(d)
        ensure_dependencies("debian", **kwargs)
        assert distros_seen == ["debian"]


# ── TestNodeSourceSetupProbe ──────────────────────────────────────────────────


class TestNodeSourceSetupProbe:
    """Boundary tests for _run_nodesource_setup's curl pre-condition check (F2)."""

    def test_nodesource_setup_fails_loudly_when_curl_absent(self):
        from unittest.mock import patch

        with patch("installer.deps_debian.shutil.which", return_value=None):
            with pytest.raises(NodeSourceSetupError, match="curl is not on PATH"):
                _run_nodesource_setup("ubuntu")

    def test_nodesource_setup_passes_when_curl_present(self):
        from unittest.mock import patch, MagicMock

        fake_result = MagicMock(returncode=0, stdout="", stderr="")
        with patch("installer.deps_debian.shutil.which", return_value="/usr/bin/curl"):
            with patch("installer._run.subprocess.run", return_value=fake_result):
                _run_nodesource_setup("ubuntu")  # must not raise


# ── TestEnsureDependenciesErrors ─────────────────────────────────────────────


class TestEnsureDependenciesErrors:
    def test_apt_update_network_error_propagates(self):
        def fail_update():
            raise AptUpdateNetworkError("network down")

        kwargs = _passing_kwargs(pkgs={"curl": False, "netcat-openbsd": True})
        kwargs["run_apt_update"] = fail_update
        with pytest.raises(AptUpdateNetworkError):
            ensure_dependencies(**kwargs)

    def test_nodesource_setup_error_propagates(self):
        def fail_nodesource(distro):
            raise NodeSourceSetupError("nodesource failed")

        kwargs = _passing_kwargs(node_version=None)
        kwargs["run_nodesource_setup"] = fail_nodesource
        with pytest.raises(NodeSourceSetupError):
            ensure_dependencies(**kwargs)

    def test_apt_install_error_propagates(self):
        def fail_install(pkgs):
            raise PackageNotFoundError("curl not found")

        kwargs = _passing_kwargs(pkgs={"curl": False, "netcat-openbsd": True})
        kwargs["run_apt_install"] = fail_install
        with pytest.raises(PackageNotFoundError):
            ensure_dependencies(**kwargs)

    def test_apt_lock_error_propagates(self):
        def fail_install(pkgs):
            raise AptLockError("apt locked")

        kwargs = _passing_kwargs(pkgs={"curl": False, "netcat-openbsd": True})
        kwargs["run_apt_install"] = fail_install
        with pytest.raises(AptLockError):
            ensure_dependencies(**kwargs)

    def test_unparseable_node_version_propagates(self):
        kwargs = _passing_kwargs(node_version="bad-version-string")
        with pytest.raises(DependencyVersionUnparseableError):
            ensure_dependencies(**kwargs)


# ── TestDependenciesConstant ──────────────────────────────────────────────────


class TestDependenciesConstant:
    def test_has_four_entries(self):
        assert len(DEPENDENCIES) == 4

    def test_required_fields_present(self):
        for entry in DEPENDENCIES:
            assert "name" in entry
            assert "packages" in entry
            assert "source" in entry
            assert "min_version" in entry

    def test_curl_entry(self):
        entry = next(e for e in DEPENDENCIES if e["name"] == "curl")
        assert entry["packages"] == ["curl"]
        assert entry["source"] == "apt-main"
        assert entry["min_version"] is None

    def test_netcat_entry(self):
        entry = next(e for e in DEPENDENCIES if e["name"] == "netcat-openbsd")
        assert entry["packages"] == ["netcat-openbsd"]
        assert entry["source"] == "apt-main"
        assert entry["min_version"] is None

    def test_docker_entry(self):
        entry = next(e for e in DEPENDENCIES if e["name"] == "docker-engine")
        assert "docker-ce" in entry["packages"]
        assert "docker-compose-plugin" in entry["packages"]
        assert entry["source"] == "get.docker.com"
        assert entry["min_version"] == (24, 0)

    def test_nodejs_entry(self):
        entry = next(e for e in DEPENDENCIES if e["name"] == "nodejs")
        assert entry["packages"] == ["nodejs"]
        assert entry["source"] == "nodesource-22"
        assert entry["min_version"] == (20, 19)


# ── TestDepsDebianBoundaryProbe ───────────────────────────────────────────────


class TestDepsDebianBoundaryProbe:
    """F7: boundary tests — run_required wraps FileNotFoundError → MissingBinaryError.

    Patches at installer._run.subprocess.run (the real OS boundary after
    migration) rather than installer.deps_debian.subprocess.run (which no
    longer exists in the module namespace post-migration).
    """

    def test_is_pkg_installed_raises_on_dpkg_absent(self):
        from unittest.mock import patch

        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("dpkg-query")):
            with pytest.raises(MissingBinaryError, match="dpkg-query"):
                _is_pkg_installed("curl")

    def test_run_apt_update_raises_on_apt_absent(self):
        from unittest.mock import patch

        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("apt-get")):
            with pytest.raises(MissingBinaryError, match="apt-get"):
                _run_apt_update()

    def test_run_apt_install_raises_on_apt_absent(self):
        from unittest.mock import patch

        with patch("installer._run.subprocess.run", side_effect=FileNotFoundError("apt-get")):
            with pytest.raises(MissingBinaryError, match="apt-get"):
                _run_apt_install(["curl"])
