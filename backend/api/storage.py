"""backend/api/storage.py

External storage source API routes.

GET  /api/storage/sources              — list all sources
POST /api/storage/sources              — add a source
GET  /api/storage/sources/{id}         — single source detail
PUT  /api/storage/sources/{id}         — update source
DELETE /api/storage/sources/{id}       — remove source
GET  /api/storage/sources/{id}/config  — generate mount config (unit + steps)
POST /api/storage/sources/{id}/verify  — test mount accessibility
GET  /api/storage/sources/{id}/files   — quick directory listing
"""

from __future__ import annotations

from typing import Any

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from backend.api.rate_limit import limiter
from backend.core.logging import get_logger
from backend.platform.storage import (
    StorageSource,
    add_source,
    generate_mount_config,
    get_source,
    list_sources,
    remove_source,
    update_source_status,
    verify_mount,
)

log = get_logger(__name__)
router = APIRouter()

VALID_TYPES = ("nfs", "smb", "rclone", "local")


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class AddSourceRequest(BaseModel):
    name: str = Field(..., description="Human label: 'Main NAS', 'Backblaze B2'")
    source_type: str = Field(..., description="nfs | smb | rclone | local")
    mount_point: str = Field(..., description="Local mount path: /mnt/nas01")
    remote_host: str | None = Field(None, description="IP or hostname (nfs/smb/sftp)")
    remote_path: str | None = Field(None, description="NFS export or SMB share name")
    is_primary: bool = Field(False, description="Use as default media_root")
    options: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Mount-type specific options. "
            "NFS: {nfs_version, extra_options}. "
            "SMB: {smb_version, uid, gid, file_mode, dir_mode}. "
            "rclone: {backend, provider, region, vfs_cache_mode, dir_cache_time}."
        ),
    )


class SourceOut(BaseModel):
    id: int | None
    name: str
    source_type: str
    remote_host: str | None
    remote_path: str | None
    mount_point: str
    is_primary: bool
    status: str
    error_message: str | None
    options: dict[str, Any]


class MountConfigOut(BaseModel):
    source_name: str
    source_type: str
    mount_point: str
    systemd_unit: str | None
    systemd_unit_name: str | None
    fstab_entry: str | None
    rclone_remote_name: str | None
    rclone_conf_block: str | None
    install_steps: list[str]
    note: str


class VerifyResult(BaseModel):
    mount_point: str
    accessible: bool
    message: str
    files_preview: list[str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _source_to_out(s: StorageSource) -> SourceOut:
    return SourceOut(
        id=s.id,
        name=s.name,
        source_type=s.source_type,
        remote_host=s.remote_host,
        remote_path=s.remote_path,
        mount_point=s.mount_point,
        is_primary=s.is_primary,
        status=s.status,
        error_message=s.error_message,
        options=s.options,
    )


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=list[SourceOut])
def list_storage_sources() -> list[SourceOut]:
    """List all configured external storage sources."""
    return [_source_to_out(s) for s in list_sources()]


@router.post("", response_model=SourceOut)
def add_storage_source(req: AddSourceRequest) -> SourceOut:
    """Add a new external storage source.

    This does NOT mount anything immediately. Call /config to generate the
    mount configuration, then follow the install steps on your host.
    """
    if req.source_type not in VALID_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"source_type must be one of: {VALID_TYPES}",
        )
    if not req.mount_point.startswith("/"):
        raise HTTPException(
            status_code=422,
            detail="mount_point must be an absolute path starting with '/'",
        )
    if req.source_type in ("nfs", "smb") and not req.remote_host:
        raise HTTPException(
            status_code=422,
            detail=f"remote_host is required for {req.source_type} sources",
        )
    if req.source_type in ("nfs", "smb") and not req.remote_path:
        raise HTTPException(
            status_code=422,
            detail=f"remote_path (export/share name) is required for {req.source_type} sources",
        )

    try:
        source = add_source(
            name=req.name,
            source_type=req.source_type,
            mount_point=req.mount_point,
            remote_host=req.remote_host,
            remote_path=req.remote_path,
            options=req.options,
            is_primary=req.is_primary,
        )
    except Exception as e:
        if "UNIQUE" in str(e):
            raise HTTPException(
                status_code=409,
                detail=f"A storage source named '{req.name}' or at '{req.mount_point}' already exists.",
            ) from e
        raise HTTPException(status_code=500, detail=str(e)) from e

    return _source_to_out(source)


@router.get("/{source_id}", response_model=SourceOut)
def get_storage_source(source_id: int) -> SourceOut:
    try:
        return _source_to_out(get_source(source_id))
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Storage source {source_id} not found.") from e


@router.delete("/{source_id}")
@limiter.limit("5/minute")  # type: ignore[untyped-decorator]  # heavy mutation — storage delete is irreversible (id=467)
def delete_storage_source(request: Request, source_id: int) -> dict[str, Any]:
    try:
        source = get_source(source_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Storage source {source_id} not found.") from e

    if source.status == "active":
        raise HTTPException(
            status_code=409,
            detail=(
                f"Storage source '{source.name}' is currently active. "
                f"Unmount it first, then delete. "
                f"Unmounting an active NAS while apps are running will cause errors."
            ),
        )

    remove_source(source_id)
    return {"ok": True, "message": f"Storage source '{source.name}' removed."}


@router.get("/{source_id}/config", response_model=MountConfigOut)
def get_mount_config(source_id: int) -> MountConfigOut:
    """Generate the mount configuration for this storage source.

    For NFS/SMB: returns systemd unit file content + fstab entry + install steps.
    For rclone: returns rclone.conf block + container command + install steps.
    For local: returns verification steps.

    Copy the systemd unit to /etc/systemd/system/ on your host and follow
    the install_steps to activate. SLOP does not modify the host OS
    directly — this is intentional.
    """
    try:
        source = get_source(source_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Storage source {source_id} not found.") from e

    cfg = generate_mount_config(source)

    return MountConfigOut(
        source_name=source.name,
        source_type=source.source_type,
        mount_point=source.mount_point,
        systemd_unit=cfg.systemd_unit,
        systemd_unit_name=cfg.systemd_unit_name,
        fstab_entry=cfg.fstab_entry,
        rclone_remote_name=cfg.rclone_remote_name,
        rclone_conf_block=cfg.rclone_conf_block,
        install_steps=cfg.install_steps,
        note=(
            "SLOP generates configuration files but does not modify your host OS. "
            "Follow the install_steps above on your server to activate this mount. "
            "NFS/SMB mounts are established at host level before Docker starts — "
            "this is more reliable than mounting inside containers."
        ),
    )


@router.post("/{source_id}/verify", response_model=VerifyResult)
def verify_storage_source(source_id: int) -> VerifyResult:
    """Test whether the storage source mount point is accessible.

    Returns a directory listing preview (up to 10 items) if accessible.
    Run this after following the install steps to confirm the mount is working.
    """
    try:
        source = get_source(source_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Storage source {source_id} not found.") from e

    accessible, message = verify_mount(source.mount_point)

    files_preview: list[str] = []
    if accessible:
        try:
            files_preview = [p.name for p in sorted(Path(source.mount_point).iterdir())[:10]]
        except Exception:  # noqa: S110  # best-effort directory listing; accessible is already True
            pass

    # Update status in DB
    new_status = "active" if accessible else "error"
    assert source.id is not None  # source loaded from DB; id is always set
    update_source_status(
        source.id,
        status=new_status,
        error=None if accessible else message,
    )

    return VerifyResult(
        mount_point=source.mount_point,
        accessible=accessible,
        message=message,
        files_preview=files_preview,
    )


@router.get("/{source_id}/files")
def list_source_files(source_id: int, path: str = "") -> dict[str, Any]:
    """Quick directory listing of the mounted storage source.

    path: relative path within the mount point (empty = root of mount).
    Returns up to 50 items with type, size, and modification time.
    """
    try:
        source = get_source(source_id)
    except KeyError as e:
        raise HTTPException(status_code=404, detail=f"Storage source {source_id} not found.") from e

    base = Path(source.mount_point)
    target = base / path.lstrip("/") if path else base

    # Security: prevent path traversal
    try:
        target.resolve().relative_to(base.resolve())
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Path traversal not allowed.") from e

    if not target.exists():
        raise HTTPException(status_code=404, detail=f"Path not found: {target}")

    try:
        entries = []
        for item in sorted(target.iterdir())[:50]:
            try:
                st = item.stat()
                entries.append(
                    {
                        "name": item.name,
                        "type": "dir" if item.is_dir() else "file",
                        "size": st.st_size if item.is_file() else None,
                        "modified": int(st.st_mtime),
                    }
                )
            except OSError:
                entries.append(
                    {"name": item.name, "type": "unknown", "size": None, "modified": None}
                )
        return {
            "mount_point": source.mount_point,
            "path": str(target.relative_to(base)),
            "entries": entries,
            "truncated": len(list(target.iterdir())) > 50,
        }
    except PermissionError as e:
        raise HTTPException(status_code=403, detail=f"Permission denied reading {target}") from e
    except OSError as e:
        raise HTTPException(status_code=500, detail=f"Could not read directory: {e}") from e
