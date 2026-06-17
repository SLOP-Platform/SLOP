"""installer/tests/test_check_readiness.py — checker behavior against synthetic archives.

Tests the check-readiness.py tool's check logic using tmp_path fixtures.
Does NOT require real VMs or a real evidence archive.

Two test tracks per Rule 5.27 two-track coverage:
1. Unit: each check type's PASS and FAIL paths in isolation.
2. Integration: check_distro() and main() entry point against a full synthetic archive.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
import yaml

# Add tools/ to path so we can import check-readiness as a module.
TOOLS_DIR = Path(__file__).parent.parent.parent / "tools"


@pytest.fixture(autouse=True, scope="module")
def add_tools_to_path() -> None:
    if str(TOOLS_DIR) not in sys.path:
        sys.path.insert(0, str(TOOLS_DIR))


# Import after path is set up.
import importlib  # noqa: E402 — import after sys.path injection for the tools/ checker module
import importlib.util  # noqa: E402 — import after sys.path injection for the tools/ checker module

_checker_spec = importlib.util.spec_from_file_location(
    "check_readiness",
    TOOLS_DIR / "check-readiness.py",
)
_checker_mod = importlib.util.module_from_spec(_checker_spec)
_checker_spec.loader.exec_module(_checker_mod)

check_distro = _checker_mod.check_distro
_run_check = _checker_mod._run_check
KNOWN_CHECK_TYPES = _checker_mod.KNOWN_CHECK_TYPES
main = _checker_mod.main

MANIFEST_PATH = Path(__file__).parent.parent / "readiness_manifest.yaml"


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture
def archive(tmp_path: Path) -> Path:
    """Minimal synthetic archive with a ubuntu_24_04 distro directory."""
    distro_dir = tmp_path / "ubuntu_24_04"
    distro_dir.mkdir()
    return tmp_path


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


# ── Unit: file_contents_equal ─────────────────────────────────────────────────


def test_file_contents_equal_pass(tmp_path: Path) -> None:
    _write(tmp_path / "exit_code", "0")
    spec = {"type": "file_contents_equal", "path": "exit_code", "expected": "0"}
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is True


def test_file_contents_equal_fail_wrong_value(tmp_path: Path) -> None:
    _write(tmp_path / "exit_code", "1")
    spec = {"type": "file_contents_equal", "path": "exit_code", "expected": "0"}
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is False
    assert "expected" in result["detail"]


def test_file_contents_equal_fail_missing_strict(tmp_path: Path) -> None:
    spec = {"type": "file_contents_equal", "path": "no_such_file", "expected": "0"}
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is False


def test_file_contents_equal_soft_skip_when_missing(tmp_path: Path) -> None:
    spec = {"type": "file_contents_equal", "path": "no_such_file", "expected": "0"}
    result = _run_check(tmp_path, spec, strict=False)
    assert result["passed"] is False
    assert result.get("skipped") is True


# ── Unit: stdout_banner_position ──────────────────────────────────────────────


def test_stdout_banner_in_final_third(tmp_path: Path) -> None:
    content = "A" * 600 + "Install complete. See /opt/slop/POST_INSTALL.txt"
    _write(tmp_path / "install.stdout.log", content)
    spec = {
        "type": "stdout_banner_position",
        "path": "install.stdout.log",
        "banner_substring": "Install complete.",
        "position": "final_third",
    }
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is True


def test_stdout_banner_in_first_third_fails(tmp_path: Path) -> None:
    content = "Install complete. See /opt/slop/POST_INSTALL.txt" + "A" * 600
    _write(tmp_path / "install.stdout.log", content)
    spec = {
        "type": "stdout_banner_position",
        "path": "install.stdout.log",
        "banner_substring": "Install complete.",
        "position": "final_third",
    }
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is False
    assert "threshold" in result["detail"]


def test_stdout_banner_position_any_passes_anywhere(tmp_path: Path) -> None:
    content = "Install complete. See /opt/slop/POST_INSTALL.txt" + "A" * 600
    _write(tmp_path / "install.stdout.log", content)
    spec = {
        "type": "stdout_banner_position",
        "path": "install.stdout.log",
        "banner_substring": "Install complete.",
        "position": "any",
    }
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is True


def test_stdout_banner_position_final_half_pass(tmp_path: Path) -> None:
    content = "A" * 600 + "Install complete. See /opt/slop/POST_INSTALL.txt"
    _write(tmp_path / "install.stdout.log", content)
    spec = {
        "type": "stdout_banner_position",
        "path": "install.stdout.log",
        "banner_substring": "Install complete.",
        "position": "final_half",
    }
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is True


def test_stdout_banner_position_final_half_fail(tmp_path: Path) -> None:
    content = "Install complete." + "A" * 600
    _write(tmp_path / "install.stdout.log", content)
    spec = {
        "type": "stdout_banner_position",
        "path": "install.stdout.log",
        "banner_substring": "Install complete.",
        "position": "final_half",
    }
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is False
    assert "threshold" in result["detail"]


def test_stdout_banner_not_found(tmp_path: Path) -> None:
    _write(tmp_path / "install.stdout.log", "A" * 100)
    spec = {
        "type": "stdout_banner_position",
        "path": "install.stdout.log",
        "banner_substring": "Install complete.",
        "position": "final_third",
    }
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is False
    assert "not found" in result["detail"]


# ── Unit: file_exists ─────────────────────────────────────────────────────────


def test_file_exists_pass(tmp_path: Path) -> None:
    _write(tmp_path / "post_install.txt", "content")
    spec = {"type": "file_exists", "path": "post_install.txt"}
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is True


def test_file_exists_fail(tmp_path: Path) -> None:
    spec = {"type": "file_exists", "path": "absent.txt"}
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is False


# ── Unit: file_nonempty ───────────────────────────────────────────────────────


def test_file_nonempty_pass(tmp_path: Path) -> None:
    _write(tmp_path / "sources.list", "deb http://ppa.launchpad.net/deadsnakes/ppa ...")
    spec = {"type": "file_nonempty", "path": "sources.list"}
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is True


def test_file_nonempty_fail_empty(tmp_path: Path) -> None:
    _write(tmp_path / "empty.txt", "")
    spec = {"type": "file_nonempty", "path": "empty.txt"}
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is False
    assert "empty" in result["detail"]


def test_file_nonempty_fail_missing(tmp_path: Path) -> None:
    spec = {"type": "file_nonempty", "path": "absent.txt"}
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is False


# ── Unit: stat_match ──────────────────────────────────────────────────────────


def test_stat_match_pass(tmp_path: Path) -> None:
    # stat -c '%a %U:%G %s' format: mode owner size
    _write(tmp_path / "post_install.stat", "644 slop:slop 1024")
    spec = {
        "type": "stat_match",
        "path": "post_install.stat",
        "mode": "644",
        "owner": "slop:slop",
    }
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is True


def test_stat_match_fail_mode(tmp_path: Path) -> None:
    _write(tmp_path / "post_install.stat", "600 slop:slop 1024")
    spec = {
        "type": "stat_match",
        "path": "post_install.stat",
        "mode": "644",
        "owner": "slop:slop",
    }
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is False
    assert "mode" in result["detail"]


def test_stat_match_fail_owner(tmp_path: Path) -> None:
    _write(tmp_path / "post_install.stat", "644 root:root 1024")
    spec = {
        "type": "stat_match",
        "path": "post_install.stat",
        "mode": "644",
        "owner": "slop:slop",
    }
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is False
    assert "owner" in result["detail"]


# ── Unit: regex_match ─────────────────────────────────────────────────────────


def test_regex_match_pass(tmp_path: Path) -> None:
    _write(tmp_path / "python3.resolution", "Python 3.11.9\n/usr/bin/python3.11")
    spec = {"type": "regex_match", "path": "python3.resolution", "pattern": r"Python 3\.(11|12|13)"}
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is True


def test_regex_match_fail(tmp_path: Path) -> None:
    _write(tmp_path / "python3.resolution", "Python 3.10.12\n/usr/bin/python3.10")
    spec = {"type": "regex_match", "path": "python3.resolution", "pattern": r"Python 3\.(11|12|13)"}
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is False
    assert "not found" in result["detail"]


# ── Unit: regex_no_match ──────────────────────────────────────────────────────


def test_regex_no_match_pass(tmp_path: Path) -> None:
    _write(tmp_path / "post_install.txt", "SLOP v5.0.0 install complete\n")
    spec = {"type": "regex_no_match", "path": "post_install.txt", "pattern": r"<[a-z_]+>"}
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is True


def test_regex_no_match_fail(tmp_path: Path) -> None:
    _write(tmp_path / "post_install.txt", "http://<hostname>:8080/\n")
    spec = {"type": "regex_no_match", "path": "post_install.txt", "pattern": r"<[a-z_]+>"}
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is False
    assert "found" in result["detail"]


# ── Unit: substring_match ─────────────────────────────────────────────────────


def test_substring_match_pass(tmp_path: Path) -> None:
    _write(tmp_path / "distro.os-release", 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"')
    spec = {
        "type": "substring_match",
        "path": "distro.os-release",
        "expected": "Debian GNU/Linux 12",
    }
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is True


def test_substring_match_fail(tmp_path: Path) -> None:
    _write(tmp_path / "distro.os-release", 'PRETTY_NAME="Ubuntu 24.04.1 LTS"')
    spec = {
        "type": "substring_match",
        "path": "distro.os-release",
        "expected": "Debian GNU/Linux 12",
    }
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is False
    assert "not found" in result["detail"]


# ── Unit: json_fields_match ───────────────────────────────────────────────────


def test_json_fields_match_pass(tmp_path: Path) -> None:
    _write(tmp_path / "state.json", json.dumps({"phase": "installed", "smoke_test_passed": True}))
    spec = {
        "type": "json_fields_match",
        "path": "state.json",
        "fields": {"phase": "installed", "smoke_test_passed": True},
    }
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is True


def test_json_fields_match_fail_wrong_value(tmp_path: Path) -> None:
    _write(tmp_path / "state.json", json.dumps({"phase": "installed", "smoke_test_passed": False}))
    spec = {
        "type": "json_fields_match",
        "path": "state.json",
        "fields": {"phase": "installed", "smoke_test_passed": True},
    }
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is False
    assert "smoke_test_passed" in result["detail"]


def test_json_fields_match_fail_missing_field(tmp_path: Path) -> None:
    _write(tmp_path / "state.json", json.dumps({"phase": "installed"}))
    spec = {
        "type": "json_fields_match",
        "path": "state.json",
        "fields": {"phase": "installed", "smoke_test_passed": True},
    }
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is False


def test_json_fields_match_fail_invalid_json(tmp_path: Path) -> None:
    _write(tmp_path / "state.json", "not json {")
    spec = {
        "type": "json_fields_match",
        "path": "state.json",
        "fields": {"phase": "installed"},
    }
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is False
    assert "JSON" in result["detail"]


# ── Unit: composite ───────────────────────────────────────────────────────────


def test_composite_all_pass(tmp_path: Path) -> None:
    _write(tmp_path / "post_install.txt", "SLOP v5.0.0 install complete\n")
    _write(tmp_path / "post_install.stat", "644 slop:slop 100")
    spec = {
        "type": "composite",
        "checks": [
            {"type": "file_exists", "path": "post_install.txt"},
            {
                "type": "stat_match",
                "path": "post_install.stat",
                "mode": "644",
                "owner": "slop:slop",
            },
            {"type": "regex_no_match", "path": "post_install.txt", "pattern": r"<[a-z_]+>"},
        ],
    }
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is True
    assert all(s["passed"] for s in result["sub_checks"])


def test_composite_one_fail(tmp_path: Path) -> None:
    _write(tmp_path / "post_install.txt", "http://<hostname>:8080/")
    _write(tmp_path / "post_install.stat", "644 slop:slop 100")
    spec = {
        "type": "composite",
        "checks": [
            {"type": "file_exists", "path": "post_install.txt"},
            {
                "type": "stat_match",
                "path": "post_install.stat",
                "mode": "644",
                "owner": "slop:slop",
            },
            {"type": "regex_no_match", "path": "post_install.txt", "pattern": r"<[a-z_]+>"},
        ],
    }
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is False
    sub_passed = [s["passed"] for s in result["sub_checks"]]
    assert sub_passed.count(False) == 1


# ── Unit: unknown check type ──────────────────────────────────────────────────


def test_unknown_check_type(tmp_path: Path) -> None:
    spec = {"type": "not_a_real_type", "path": "anything"}
    result = _run_check(tmp_path, spec, strict=True)
    assert result["passed"] is False
    assert "unknown check type" in result["detail"]


# ── Integration: check_distro with real manifest + synthetic archive ───────────


@pytest.fixture
def real_manifest() -> dict:
    assert MANIFEST_PATH.exists()
    with open(MANIFEST_PATH) as f:
        return yaml.safe_load(f)


def _make_ubuntu_24_04_archive(distro_dir: Path) -> None:
    """Populate a synthetic ubuntu_24_04 evidence archive that passes all checks."""
    # install.exit_code
    _write(distro_dir / "install.exit_code", "0")
    # install.stdout.log: banner in final third
    preamble = "Step 1/5: installing...\n" * 50
    banner = "==================================================\nInstall complete. See /opt/slop/POST_INSTALL.txt\n"
    _write(distro_dir / "install.stdout.log", preamble + banner)
    # smoke_invocation.evidence
    _write(
        distro_dir / "smoke_invocation.evidence", "Install complete. See /opt/slop/POST_INSTALL.txt"
    )
    # post_install.txt: no placeholders
    _write(
        distro_dir / "post_install.txt",
        "SLOP v5.0.0 install complete\n\nhttp://192.168.1.100:8080/\n",
    )
    # post_install.stat: 644 slop:slop
    _write(distro_dir / "post_install.stat", "644 slop:slop 512")
    # post_install.placeholders
    _write(distro_dir / "post_install.placeholders", "no matches")
    # state.json
    _write(distro_dir / "state.json", json.dumps({"phase": "installed", "smoke_test_passed": True}))
    _write(distro_dir / "state.json.fields", "phase=installed smoke_test_passed=true")
    # smoke_rerun.json
    _write(distro_dir / "smoke_rerun.json", json.dumps({"predicate": "all", "passed": True}))
    # install.stderr.log (empty is fine)
    _write(distro_dir / "install.stderr.log", "")


def test_check_distro_ubuntu_24_04_pass(tmp_path: Path, real_manifest: dict) -> None:
    distro_dir = tmp_path / "ubuntu_24_04"
    distro_dir.mkdir()
    _make_ubuntu_24_04_archive(distro_dir)
    result = check_distro(real_manifest, tmp_path, "ubuntu_24_04", strict=True)
    assert result["overall"] == "PASS", f"Expected PASS, got FAIL: {result}"
    assert all(v["passed"] for v in result["core_predicates"].values())


def test_check_distro_ubuntu_24_04_fail_on_exit_code(tmp_path: Path, real_manifest: dict) -> None:
    distro_dir = tmp_path / "ubuntu_24_04"
    distro_dir.mkdir()
    _make_ubuntu_24_04_archive(distro_dir)
    # Overwrite exit_code to indicate a failure.
    _write(distro_dir / "install.exit_code", "1")
    result = check_distro(real_manifest, tmp_path, "ubuntu_24_04", strict=True)
    assert result["overall"] == "FAIL"
    assert not result["core_predicates"]["installer_exit_0"]["passed"]


def test_check_distro_missing_evidence_strict(tmp_path: Path, real_manifest: dict) -> None:
    distro_dir = tmp_path / "ubuntu_24_04"
    distro_dir.mkdir()
    # Only provide exit_code; all others missing.
    _write(distro_dir / "install.exit_code", "0")
    result = check_distro(real_manifest, tmp_path, "ubuntu_24_04", strict=True)
    assert result["overall"] == "FAIL"


def test_check_distro_missing_directory(tmp_path: Path, real_manifest: dict) -> None:
    result = check_distro(real_manifest, tmp_path, "ubuntu_24_04", strict=True)
    assert result["overall"] == "FAIL"
    assert "not found" in result["detail"]


def test_check_distro_debian_12_pass(tmp_path: Path, real_manifest: dict) -> None:
    distro_dir = tmp_path / "debian_12"
    distro_dir.mkdir()
    _make_ubuntu_24_04_archive(distro_dir)  # core files are the same
    # Add debian-specific captures.
    _write(distro_dir / "distro.os-release", 'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"')
    _write(distro_dir / "python3.resolution", "Python 3.11.9\n/usr/bin/python3.11")
    _write(distro_dir / "data_dir.ls", "drwxr-x--- 2 slop slop 4096 May 16 12:00 /var/lib/slop")
    _write(
        distro_dir / "apt.python.dpkg", "ii  python3-venv  3.11.9 all\nii  python3  3.11.9 amd64"
    )
    result = check_distro(real_manifest, tmp_path, "debian_12", strict=True)
    assert result["overall"] == "PASS", f"Expected PASS: {result}"


def test_check_distro_ubuntu_22_04_pass(tmp_path: Path, real_manifest: dict) -> None:
    distro_dir = tmp_path / "ubuntu_22_04"
    distro_dir.mkdir()
    _make_ubuntu_24_04_archive(distro_dir)
    _write(distro_dir / "distro.os-release", 'PRETTY_NAME="Ubuntu 22.04.4 LTS"')
    _write(
        distro_dir / "apt.sources.deadsnakes",
        "deb http://ppa.launchpad.net/deadsnakes/ppa/ubuntu jammy main",
    )
    _write(
        distro_dir / "python3.resolution",
        "Python 3.11.9\n/usr/bin/python3.11\npython3 - priority 1",
    )
    _write(
        distro_dir / "apt.python311.dpkg",
        "ii  python3.11-venv  3.11.9 amd64\nii  python3.11  3.11.9 amd64",
    )
    _write(distro_dir / "data_dir.ls", "drwxr-x--- 2 slop slop 4096 May 16 12:00 /var/lib/slop")
    _write(
        distro_dir / "systemd.unit",
        "ExecStart=/opt/slop/.venv/bin/uvicorn backend.api.main:app --host 0.0.0.0 --port 8080\nEnvironment=MS_DATA_DIR=/var/lib/slop",
    )
    # ^ actual service template uses uvicorn, not python3 (design note: pattern corrected at Commit 2)
    result = check_distro(real_manifest, tmp_path, "ubuntu_22_04", strict=True)
    assert result["overall"] == "PASS", f"Expected PASS: {result}"


def test_check_distro_ubuntu_22_04_fail_no_deadsnakes(tmp_path: Path, real_manifest: dict) -> None:
    distro_dir = tmp_path / "ubuntu_22_04"
    distro_dir.mkdir()
    _make_ubuntu_24_04_archive(distro_dir)
    _write(distro_dir / "distro.os-release", 'PRETTY_NAME="Ubuntu 22.04.4 LTS"')
    _write(distro_dir / "apt.sources.deadsnakes", "")  # empty = deadsnakes NOT added
    _write(distro_dir / "python3.resolution", "Python 3.11.9\n/usr/bin/python3.11")
    _write(distro_dir / "apt.python311.dpkg", "ii  python3.11-venv  3.11.9 amd64")
    _write(distro_dir / "data_dir.ls", "drwxr-x--- 2 slop slop 4096 May 16 12:00 /var/lib/slop")
    _write(
        distro_dir / "systemd.unit",
        "ExecStart=/opt/slop/.venv/bin/uvicorn backend.api.main:app --host 0.0.0.0 --port 8080",
    )
    result = check_distro(real_manifest, tmp_path, "ubuntu_22_04", strict=True)
    assert result["overall"] == "FAIL"
    assert not result["distro_captures"]["deadsnakes_ppa_added"]["passed"]


# ── Integration: sha256 evidence hashes ───────────────────────────────────────


def test_evidence_hash_in_result(tmp_path: Path, real_manifest: dict) -> None:
    distro_dir = tmp_path / "ubuntu_24_04"
    distro_dir.mkdir()
    _make_ubuntu_24_04_archive(distro_dir)
    result = check_distro(real_manifest, tmp_path, "ubuntu_24_04", strict=True)
    # At least one predicate should have an evidence_hash.
    hashes = [
        v.get("evidence_hash") for v in result["core_predicates"].values() if v.get("evidence_hash")
    ]
    assert hashes, "No evidence hashes found in result"
    assert all(h.startswith("sha256:") for h in hashes)
