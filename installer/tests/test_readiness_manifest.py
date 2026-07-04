"""installer/tests/test_readiness_manifest.py — schema-parse and validity tests.

Verifies that installer/readiness_manifest.yaml:
  - Parses as valid YAML
  - Has schema_version == 1
  - All core_predicates have a check.type that is a known type
  - All distro_evidence keys are known distros
  - All distro capture checks have a known type
  - predicted_classes entries have required fields

Does NOT require real VMs or a real archive; tests the manifest structure only.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

# Import known check types from the checker tool.
sys_path_add = None  # resolved below via importlib

MANIFEST_PATH = Path(__file__).parent.parent / "readiness_manifest.yaml"

KNOWN_CHECK_TYPES = {
    "file_contents_equal",
    "stdout_banner_position",
    "file_exists",
    "file_nonempty",
    "stat_match",
    "regex_match",
    "regex_no_match",
    "substring_match",
    "json_fields_match",
    "json_field_match",
    "exit_code",
    "composite",
}

KNOWN_DISTROS = {"ubuntu_24_04", "debian_13", "debian_12"}


@pytest.fixture(scope="module")
def manifest() -> dict:
    assert MANIFEST_PATH.exists(), f"manifest not found: {MANIFEST_PATH}"
    with open(MANIFEST_PATH) as f:
        return yaml.safe_load(f)


# ── Top-level schema ──────────────────────────────────────────────────────────


def test_schema_version(manifest: dict) -> None:
    assert manifest["schema_version"] == 1


def test_adr_reference_present(manifest: dict) -> None:
    assert "adr_reference" in manifest
    assert "0015" in manifest["adr_reference"]


def test_audit_gate_invariants(manifest: dict) -> None:
    invariants = manifest["audit_gate_invariants"]
    assert "INV-7" in invariants
    assert "INV-8" in invariants
    assert "INV-9" in invariants


# ── core_predicates ───────────────────────────────────────────────────────────


def test_core_predicates_present(manifest: dict) -> None:
    assert "core_predicates" in manifest
    assert len(manifest["core_predicates"]) >= 5


def test_core_predicate_ids_unique(manifest: dict) -> None:
    ids = [p["id"] for p in manifest["core_predicates"]]
    assert len(ids) == len(set(ids)), f"duplicate predicate IDs: {ids}"


@pytest.mark.parametrize(
    "pred_id",
    [
        "installer_exit_0",
        "smoke_test_invoked",
        "post_install_inv8",
        "state_file_inv9",
        "smoke_rerun_all_pass",
    ],
)
def test_required_core_predicates_present(manifest: dict, pred_id: str) -> None:
    ids = [p["id"] for p in manifest["core_predicates"]]
    assert pred_id in ids, f"required predicate {pred_id!r} not found in manifest"


def _collect_check_types(check: dict) -> list:
    """Recursively collect all check types in a check spec (handles composite)."""
    types = [check.get("type")]
    for sub in check.get("checks", []):
        types.extend(_collect_check_types(sub))
    return types


@pytest.mark.parametrize("predicate", [None])
def test_core_predicate_check_types_known(manifest: dict, predicate: None) -> None:
    for pred in manifest["core_predicates"]:
        check = pred.get("check", {})
        for ct in _collect_check_types(check):
            assert ct in KNOWN_CHECK_TYPES, (
                f"predicate {pred['id']!r} has unknown check type {ct!r}"
            )


def test_core_predicate_has_evidence_paths(manifest: dict) -> None:
    for pred in manifest["core_predicates"]:
        assert "evidence_paths" in pred, f"predicate {pred['id']!r} missing evidence_paths"
        assert isinstance(pred["evidence_paths"], list)
        assert len(pred["evidence_paths"]) >= 1


# ── distro_evidence ───────────────────────────────────────────────────────────


def test_distro_evidence_present(manifest: dict) -> None:
    assert "distro_evidence" in manifest


def test_distro_evidence_keys_are_known_distros(manifest: dict) -> None:
    keys = set(manifest["distro_evidence"].keys())
    unknown = keys - KNOWN_DISTROS
    assert not unknown, f"unknown distros in distro_evidence: {unknown}"


def test_all_known_distros_present(manifest: dict) -> None:
    keys = set(manifest["distro_evidence"].keys())
    missing = KNOWN_DISTROS - keys
    assert not missing, f"known distros missing from distro_evidence: {missing}"


def test_distro_capture_check_types_known(manifest: dict) -> None:
    for distro, info in manifest["distro_evidence"].items():
        for capture in info.get("captures", []):
            check = capture.get("check", {})
            for ct in _collect_check_types(check):
                assert ct in KNOWN_CHECK_TYPES, (
                    f"distro {distro!r} capture {capture.get('id')!r} has unknown check type {ct!r}"
                )


def test_ubuntu_24_04_has_no_captures(manifest: dict) -> None:
    captures = manifest["distro_evidence"]["ubuntu_24_04"].get("captures", [])
    assert captures == [], "ubuntu_24_04 should have no distro-specific captures (baseline)"


def test_debian_12_has_required_captures(manifest: dict) -> None:
    ids = {c["id"] for c in manifest["distro_evidence"]["debian_12"].get("captures", [])}
    for required in (
        "distro_version_check",
        "python3_resolution",
        "data_dir_ownership",
        "apt_python_package",
    ):
        assert required in ids, f"debian_12 missing capture {required!r}"


# ── archived_distros ─────────────────────────────────────────────────────────


def test_ubuntu_22_04_is_archived(manifest: dict) -> None:
    """ubuntu_22_04 was moved to archived_distros per ADR 0016 (Shape B)."""
    assert "archived_distros" in manifest
    assert "ubuntu_22_04" in manifest["archived_distros"], (
        "ubuntu_22_04 not found in archived_distros — archival per ADR 0016 not recorded"
    )
    entry = manifest["archived_distros"]["ubuntu_22_04"]
    for required_field in ("archived_at", "archived_reason", "adr_reference"):
        assert required_field in entry, (
            f"ubuntu_22_04 archived entry missing field {required_field!r}"
        )
    ids = {c["id"] for c in entry.get("captures", [])}
    for required_capture in (
        "deadsnakes_ppa_added",
        "python3_resolution",
        "apt_python311_package",
        "systemd_python_path",
    ):
        assert required_capture in ids, (
            f"ubuntu_22_04 archived captures missing {required_capture!r}"
        )


# ── predicted_classes ─────────────────────────────────────────────────────────


def test_predicted_classes_present(manifest: dict) -> None:
    assert "predicted_classes" in manifest
    assert len(manifest["predicted_classes"]) >= 5


@pytest.mark.parametrize("class_id", ["F", "F16", "I", "D", "S"])
def test_required_predicted_classes_present(manifest: dict, class_id: str) -> None:
    ids = {pc["class_id"] for pc in manifest["predicted_classes"]}
    assert class_id in ids, f"predicted class {class_id!r} not found"


def test_predicted_class_required_fields(manifest: dict) -> None:
    for pc in manifest["predicted_classes"]:
        assert "class_id" in pc
        assert "description" in pc
        assert "expected_to_fire" in pc
        assert isinstance(pc["expected_to_fire"], bool)
        assert "monitored_distros" in pc
        assert "catch_predicate" in pc
