"""installer/state.py — installer state-file schema, reader, and writer.

The state file is the canonical record of what the installer did.
It is written atomically via temp-file-and-rename; callers always use
read_state_file() and write_state_file(), never access the file directly.

Schema, lifecycle, and error semantics are defined in
docs/adr/0013-installer-layout-contract.md §2.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

SUPPORTED_SCHEMA_VERSION: int = 1
STATE_FILE_NAME: str = ".installer-state.json"

_KNOWN_FIELDS = frozenset(
    {
        "schema_version",
        "slop_version",
        "phase",
        "started_at",
        "completed_at",
        "install_dir",
        "data_dir",
        "install_user",
        "distro",
        "distro_version",
        "port",
        "smoke_test_passed",
    }
)

_VALID_PHASES = frozenset({"installing", "installed"})


class StateFileCorruptedError(Exception):
    """Raised when the state file exists but fails validation.

    Callers surface the message verbatim; it names the validation failure.
    --force does NOT override this; see ADR 0013 §4 S4 rationale.
    """


class StateFileNewerSchemaError(Exception):
    """Raised when state file's schema_version exceeds SUPPORTED_SCHEMA_VERSION."""


@dataclass
class StateFile:
    """In-memory representation of .installer-state.json (ADR 0013 §2 schema)."""

    schema_version: int
    slop_version: str
    phase: str  # "installing" | "installed"
    started_at: str  # ISO 8601 UTC
    completed_at: str | None  # ISO 8601 UTC; null while phase="installing"
    install_dir: str  # absolute path; echoes --install-dir or its default
    data_dir: str  # absolute path; echoes --data-dir or its default
    install_user: str  # username of the provisioned system user
    distro: str  # /etc/os-release ID= value
    distro_version: str  # /etc/os-release VERSION_ID= value
    port: int  # HTTP port the backend binds to
    smoke_test_passed: bool


def read_state_file(path: str | Path) -> StateFile | None:
    """Read and validate the installer state file at *path*.

    Returns None if the file does not exist (clean-host or missing state).
    Raises StateFileNewerSchemaError if schema_version > SUPPORTED_SCHEMA_VERSION.
    Raises StateFileCorruptedError for any other parse or validation failure.

    This is the single point of access for the state file (ADR 0013 §2 Readers).
    No other code reads the file directly.
    """
    p = Path(path)
    if not p.exists():
        return None

    try:
        raw = p.read_text(encoding="utf-8")
    except PermissionError:
        raise  # Callers (uninstall §A.3.5) handle this separately from corruption
    except OSError as exc:
        raise StateFileCorruptedError(f"Cannot read state file at {p}: {exc}") from exc

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise StateFileCorruptedError(f"State file at {p} is not valid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise StateFileCorruptedError(
            f"State file at {p} must be a JSON object; got {type(data).__name__}"
        )

    # Check schema_version before unknown-field rejection so version mismatch
    # is reported precisely rather than as a generic "unknown field" error.
    schema_version = data.get("schema_version")
    if not isinstance(schema_version, int) or isinstance(schema_version, bool):
        raise StateFileCorruptedError(
            f"State file at {p}: 'schema_version' must be an integer; "
            f"got {type(schema_version).__name__!r}"
        )
    if schema_version > SUPPORTED_SCHEMA_VERSION:
        raise StateFileNewerSchemaError(
            f"State file at {p} was written by a newer installer "
            f"(schema version {schema_version}; this installer supports up to "
            f"{SUPPORTED_SCHEMA_VERSION}). Upgrade the installer, or restore a "
            f"state file from a matching version."
        )

    # Reject unknown fields (ADR 0013 §2: unknown fields → corrupted).
    unknown = set(data.keys()) - _KNOWN_FIELDS
    if unknown:
        raise StateFileCorruptedError(
            f"State file at {p} contains unknown field(s): {sorted(unknown)}. "
            f"Remove or correct the state file."
        )

    # Validate required string fields.
    for field in (
        "slop_version",
        "phase",
        "started_at",
        "install_dir",
        "data_dir",
        "install_user",
        "distro",
        "distro_version",
    ):
        val = data.get(field)
        if not isinstance(val, str):
            raise StateFileCorruptedError(
                f"State file at {p}: '{field}' must be a string; got {type(val).__name__!r}"
            )

    phase = data["phase"]
    if phase not in _VALID_PHASES:
        raise StateFileCorruptedError(
            f"State file at {p}: 'phase' must be one of {sorted(_VALID_PHASES)}; got {phase!r}"
        )

    port = data.get("port")
    if not isinstance(port, int) or isinstance(port, bool):
        raise StateFileCorruptedError(
            f"State file at {p}: 'port' must be an integer; got {type(port).__name__!r}"
        )

    smoke = data.get("smoke_test_passed")
    if not isinstance(smoke, bool):
        raise StateFileCorruptedError(
            f"State file at {p}: 'smoke_test_passed' must be a boolean; "
            f"got {type(smoke).__name__!r}"
        )

    completed_at = data.get("completed_at")
    if completed_at is not None and not isinstance(completed_at, str):
        raise StateFileCorruptedError(
            f"State file at {p}: 'completed_at' must be a string or null; "
            f"got {type(completed_at).__name__!r}"
        )

    return StateFile(
        schema_version=schema_version,
        slop_version=data["slop_version"],
        phase=phase,
        started_at=data["started_at"],
        completed_at=completed_at,
        install_dir=data["install_dir"],
        data_dir=data["data_dir"],
        install_user=data["install_user"],
        distro=data["distro"],
        distro_version=data["distro_version"],
        port=port,
        smoke_test_passed=smoke,
    )


def write_state_file(state: StateFile, path: str | Path) -> None:
    """Write *state* to *path* atomically (temp-file + os.replace on same FS).

    The containing directory is created if absent. Final content is UTF-8
    JSON, 2-space indented, with a trailing newline (ADR 0013 §2 Format).
    """
    p = Path(path)
    content = json.dumps(asdict(state), indent=2) + "\n"

    dir_ = p.parent
    dir_.mkdir(parents=True, exist_ok=True)
    # ADR 0013 §1: final-state mode 0640. The chown/ownership model stays PARKED
    # (three models evaluated in docs/cleanup/TIER_4_HANDOFF.md §O2 — out of scope
    # here); only the mode is enforced now.

    fd, tmp_path = tempfile.mkstemp(dir=dir_, prefix=".installer-state.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, p)
        os.chmod(p, 0o640)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
