"""backend/platform/storage.py

External storage source manager.

Handles two categories:

  LAN NAS (nfs / smb)
    → Generates systemd .mount unit files installed on the host.
    → Docker sees them as ordinary local paths (bind mounts).
    → No container needed — kernel handles the connection.

  Cloud / remote (rclone)
    → Generates rclone.conf remote definitions.
    → Deploys an rclone container instance per source.
    → FUSE mount shared via rshared propagation.

  Local paths (local)
    → Already mounted on the host. SLOP just records the path.

Multiple sources can be defined. The primary source is used as media_root.
If MergerFS is present on the host, SLOP can optionally unify all
NAS sources into a single merged mount.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.core.logging import get_logger
from backend.core.state import StateDB

# Pure mount-unit / rclone config generation lives in a sibling module to keep this file under the
# production_code cap (#1302). Imported back + re-exported (redundant-alias idiom) so existing
# callers — storage.generate_mount_config / .generate_nfs_unit / .MountConfig /
# ._sanitize_path_component … — keep resolving unchanged.
from backend.platform.storage_units import (
    MountConfig as MountConfig,
    _sanitize_path_component as _sanitize_path_component,
    _systemd_unit_name as _systemd_unit_name,
    generate_mount_config as generate_mount_config,
    generate_nfs_unit as generate_nfs_unit,
    generate_rclone_config as generate_rclone_config,
    generate_smb_unit as generate_smb_unit,
)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass
class StorageSource:
    id: int | None
    name: str
    source_type: str  # nfs | smb | rclone | local
    remote_host: str | None  # IP or hostname
    remote_path: str | None  # NFS export or SMB share name
    mount_point: str  # /mnt/nas01
    credentials_key: str | None
    options: dict[str, Any]  # parsed from JSON
    is_primary: bool
    status: str  # active | inactive | error | mounting
    error_message: str | None


# ---------------------------------------------------------------------------
# State DB CRUD for storage sources
# ---------------------------------------------------------------------------


def add_source(
    name: str,
    source_type: str,
    mount_point: str,
    remote_host: str | None = None,
    remote_path: str | None = None,
    options: dict[str, Any] | None = None,
    credentials_key: str | None = None,
    is_primary: bool = False,
) -> StorageSource:
    with StateDB() as db:
        cur = db._c.execute(
            """
            INSERT INTO storage_sources
                (name, source_type, remote_host, remote_path, mount_point,
                 credentials_key, options, is_primary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                source_type,
                remote_host,
                remote_path,
                mount_point,
                credentials_key,
                json.dumps(options or {}),
                1 if is_primary else 0,
            ),
        )
        row_id = cur.lastrowid
    if row_id is None:
        raise RuntimeError(
            "INSERT INTO storage_sources did not populate lastrowid — DB error or driver mismatch."
        )
    return get_source(row_id)


def get_source(source_id: int) -> StorageSource:
    with StateDB() as db:
        try:
            row = db._c.execute("SELECT * FROM storage_sources WHERE id=?", (source_id,)).fetchone()
        except OverflowError as e:
            # An id outside SQLite's signed-64-bit range can never match a stored row;
            # surface it as not-found (KeyError → 404) rather than an uncaught 500 (#1197).
            raise KeyError(f"Storage source {source_id} not found") from e
    if not row:
        raise KeyError(f"Storage source {source_id} not found")
    return _row_to_source(row)


def list_sources() -> list[StorageSource]:
    with StateDB() as db:
        rows = db._c.execute(
            "SELECT * FROM storage_sources ORDER BY is_primary DESC, name"
        ).fetchall()
    return [_row_to_source(r) for r in rows]


def update_source_status(source_id: int, status: str, error: str | None = None) -> None:
    with StateDB() as db:
        db._c.execute(
            "UPDATE storage_sources SET status=?, error_message=?, updated_at=unixepoch() WHERE id=?",
            (status, error, source_id),
        )


def remove_source(source_id: int) -> None:
    with StateDB() as db:
        db._c.execute("DELETE FROM storage_sources WHERE id=?", (source_id,))


def _row_to_source(row: Any) -> StorageSource:
    cols = [
        "id",
        "name",
        "source_type",
        "remote_host",
        "remote_path",
        "mount_point",
        "credentials_key",
        "options",
        "is_primary",
        "status",
        "error_message",
        "created_at",
        "updated_at",
    ]
    d = dict(zip(cols, row, strict=False)) if not hasattr(row, "keys") else dict(row)
    return StorageSource(
        id=d.get("id"),
        name=d["name"],
        source_type=d["source_type"],
        remote_host=d.get("remote_host"),
        remote_path=d.get("remote_path"),
        mount_point=d["mount_point"],
        credentials_key=d.get("credentials_key"),
        options=json.loads(d.get("options") or "{}"),
        is_primary=bool(d.get("is_primary", 0)),
        status=d.get("status", "inactive"),
        error_message=d.get("error_message"),
    )


# ---------------------------------------------------------------------------
# Mount verification
# ---------------------------------------------------------------------------


def verify_mount(mount_point: str) -> tuple[bool, str]:
    """Check if a path is mounted and accessible.

    Returns (is_mounted, message).
    """
    path = Path(mount_point)
    if not path.exists():
        return False, f"Mount point {mount_point} does not exist."

    try:
        # Check if it's a mount point (different device from parent)
        parent_stat = os.stat(str(path.parent))
        mount_stat = os.stat(str(path))
        is_mountpoint = mount_stat.st_dev != parent_stat.st_dev
    except OSError as e:
        return False, f"Could not stat {mount_point}: {e}"

    if not is_mountpoint:
        # Could still be a bind mount or local — check if readable
        try:
            list(path.iterdir())
            return True, f"{mount_point} is accessible (local path)."
        except PermissionError:
            return False, f"{mount_point} exists but is not readable."
        except OSError as e:
            return False, f"{mount_point} exists but is not accessible: {e}"

    # It's a mount point — check it's actually readable (not stale)
    try:
        list(path.iterdir())
        return True, f"{mount_point} is mounted and readable."
    except OSError as e:
        return False, f"{mount_point} is mounted but not accessible (stale mount?): {e}"


def primary_media_root() -> str | None:
    """Return the mount_point of the primary storage source, or None."""
    try:
        sources = list_sources()
        primary = next((s for s in sources if s.is_primary and s.status == "active"), None)
        return primary.mount_point if primary else None
    except Exception:
        return None
