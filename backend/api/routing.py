"""backend/api/routing.py

Request routing API — multi-instance app management and
debrid vs. download routing configuration.

GET  /api/routing/instances                     — list all app instances
GET  /api/routing/instances/{manifest_key}      — instances for one app
POST /api/routing/instances/{manifest_key}      — install a new instance
DELETE /api/routing/instances/{instance_key}    — remove an instance

GET  /api/routing/media                         — list routing config per media type
PUT  /api/routing/media/{media_type}            — update routing for one type
GET  /api/routing/media/{media_type}/seerr-help — Seerr config instructions
"""

from __future__ import annotations

from typing import Any


from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from backend.core.logging import get_logger
from backend.core.state import StateDB
from backend.manifests.executor import (
    InstanceResult,
    get_instances_for_manifest,
    install_instance,
    list_instances,
    remove_app,
)

log = get_logger(__name__)
router = APIRouter()

VALID_ROLES = ("default", "debrid", "download", "secondary")
VALID_MEDIA_TYPES = ("movies", "tv", "music", "books", "comics", "audiobooks", "adult")

# Maps media_type → canonical manifest that handles it
MEDIA_TYPE_MANIFEST: dict[str, str] = {
    "movies": "radarr",
    "tv": "sonarr",
    "music": "lidarr",
    "books": "readarr",
    "comics": "mylar3",
    "audiobooks": "readarr",
    "adult": "whisparr",
}

# Seerr only supports movies + tv natively
SEERR_SUPPORTED = {"movies", "tv"}


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


class InstallInstanceRequest(BaseModel):
    instance_key: str = Field(
        ...,
        description="Unique key for this instance, e.g. 'radarr_debrid'",
        pattern=r"^[a-z][a-z0-9_]*$",
    )
    label: str = Field(
        ...,
        description="Human label shown in UI, e.g. 'Radarr (Debrid)'",
    )
    role: str = Field(
        "default",
        description="Role: default | debrid | download | secondary",
    )
    host_port: int | None = Field(None, description="Override internal port for host binding")
    extra_env: dict[str, str] = Field(default_factory=dict)


class InstanceOut(BaseModel):
    instance_key: str
    manifest_key: str
    label: str
    role: str
    status: str
    host_port: int | None
    web_port: int | None


class RoutingConfig(BaseModel):
    media_type: str
    canonical_manifest: str
    debrid_instance: str | None
    download_instance: str | None
    seerr_debrid_id: int | None
    seerr_download_id: int | None
    default_path: str
    seerr_supported: bool
    notes: str | None


class UpdateRoutingRequest(BaseModel):
    debrid_instance: str | None = Field(None, description="Instance key of the debrid arr")
    download_instance: str | None = Field(None, description="Instance key of the download arr")
    seerr_debrid_id: int | None = Field(None, description="Seerr's internal ID for debrid instance")
    seerr_download_id: int | None = Field(
        None, description="Seerr's internal ID for download instance"
    )
    default_path: str = Field("download", description="debrid | download | ask")
    notes: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_routing(media_type: str) -> dict[str, Any]:
    with StateDB() as db:
        row = db._c.execute(
            "SELECT * FROM request_routing WHERE media_type=?", (media_type,)
        ).fetchone()
    if not row:
        raise KeyError(media_type)
    cols = [
        "id",
        "media_type",
        "debrid_instance",
        "download_instance",
        "seerr_debrid_id",
        "seerr_download_id",
        "default_path",
        "notes",
        "updated_at",
    ]
    return dict(zip(cols, row, strict=False))


def _all_routing() -> list[dict[str, Any]]:
    with StateDB() as db:
        rows = db._c.execute("SELECT * FROM request_routing ORDER BY media_type").fetchall()
    cols = [
        "id",
        "media_type",
        "debrid_instance",
        "download_instance",
        "seerr_debrid_id",
        "seerr_download_id",
        "default_path",
        "notes",
        "updated_at",
    ]
    return [dict(zip(cols, r, strict=False)) for r in rows]


def _routing_to_out(r: dict[str, Any]) -> RoutingConfig:
    return RoutingConfig(
        media_type=r["media_type"],
        canonical_manifest=MEDIA_TYPE_MANIFEST.get(r["media_type"], "unknown"),
        debrid_instance=r.get("debrid_instance"),
        download_instance=r.get("download_instance"),
        seerr_debrid_id=r.get("seerr_debrid_id"),
        seerr_download_id=r.get("seerr_download_id"),
        default_path=r.get("default_path", "download"),
        seerr_supported=r["media_type"] in SEERR_SUPPORTED,
        notes=r.get("notes"),
    )


# ---------------------------------------------------------------------------
# Instance routes
# ---------------------------------------------------------------------------


@router.get("/instances", response_model=list[InstanceOut])
def get_all_instances() -> list[InstanceOut]:
    """List all installed app instances (base installs + named instances)."""
    return [InstanceOut(**i) for i in list_instances()]


@router.get("/instances/{manifest_key}", response_model=list[InstanceOut])
def get_manifest_instances(manifest_key: str) -> list[InstanceOut]:
    """List all instances of a specific manifest (e.g. all radarr instances)."""
    instances = get_instances_for_manifest(manifest_key)
    if not instances:
        # Also check if the base app is installed under the manifest_key itself
        with StateDB() as db:
            app = db.get_app(manifest_key)
        if app:
            return [
                InstanceOut(
                    instance_key=manifest_key,
                    manifest_key=manifest_key,
                    label=app.display_name,
                    role="default",
                    status=app.status,
                    host_port=app.host_port,
                    web_port=app.web_port,
                )
            ]
    return [InstanceOut(**i) for i in instances]


@router.post("/instances/{manifest_key}", response_model=InstanceOut)
def install_app_instance(
    manifest_key: str,
    req: InstallInstanceRequest,
) -> InstanceOut:
    """Install a named instance of an app manifest.

    Use this to run Radarr (Debrid) alongside Radarr (Download), or
    Sonarr (Debrid) alongside Sonarr (Download).

    The instance_key must be unique across all installed apps.
    Example flow:

        # Install base Radarr (download path)
        POST /api/apps/radarr/install

        # Install Radarr (debrid path)
        POST /api/routing/instances/radarr
        {
          "instance_key": "radarr_debrid",
          "label": "Radarr (Debrid)",
          "role": "debrid",
          "host_port": 7879
        }

        # Update routing so movies default to debrid
        PUT /api/routing/media/movies
        {
          "debrid_instance": "radarr_debrid",
          "download_instance": "radarr",
          "default_path": "debrid"
        }
    """
    if req.role not in VALID_ROLES:
        raise HTTPException(
            status_code=422,
            detail=f"role must be one of: {VALID_ROLES}",
        )

    # Verify the manifest exists in the catalog before attempting install
    try:
        from backend.manifests.loader import load_manifest

        load_manifest(manifest_key)
    except Exception as err:
        raise HTTPException(
            status_code=404,
            detail=f"No manifest found for '{manifest_key}'. Check the catalog for available app keys.",
        ) from err

    result: InstanceResult = install_instance(
        manifest_key=manifest_key,
        instance_key=req.instance_key,
        instance_label=req.label,
        role=req.role,
        extra_env=req.extra_env,
        host_port_override=req.host_port,
    )

    if not result.ok:
        raise HTTPException(status_code=500, detail=result.error)

    # Return the installed instance info
    with StateDB() as db:
        app = db.get_app(req.instance_key)

    return InstanceOut(
        instance_key=req.instance_key,
        manifest_key=manifest_key,
        label=req.label,
        role=req.role,
        status=app.status if app else "unknown",
        host_port=app.host_port if app else None,
        web_port=app.web_port if app else None,
    )


@router.delete("/instances/{instance_key}")
def remove_instance(instance_key: str, delete_config: bool = False) -> dict[str, Any]:
    """Remove a named instance."""
    # Remove from app_instances table first
    with StateDB() as db:
        db._c.execute("DELETE FROM app_instances WHERE instance_key=?", (instance_key,))
        # NOTE: StateDB auto-commits on __exit__ — db._c.commit() removed (Core Rule 4.4)

    result = remove_app(instance_key, delete_config=delete_config)
    if not result.ok:
        err = result.error or "removal failed"
        status = 404 if "not installed" in err.lower() or "not found" in err.lower() else 500
        raise HTTPException(status_code=status, detail=err)

    return {"ok": True, "instance_key": instance_key, "message": "Instance removed."}


# ---------------------------------------------------------------------------
# Routing config routes
# ---------------------------------------------------------------------------


@router.get("/media", response_model=list[RoutingConfig])
def get_all_routing() -> list[RoutingConfig]:
    """Return the routing configuration for all media types."""
    return [_routing_to_out(r) for r in _all_routing()]


@router.get("/media/{media_type}", response_model=RoutingConfig)
def get_media_routing(media_type: str) -> RoutingConfig:
    """Get routing configuration for a specific media type."""
    if media_type not in VALID_MEDIA_TYPES:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown media type '{media_type}'. Valid: {VALID_MEDIA_TYPES}",
        )
    try:
        return _routing_to_out(_load_routing(media_type))
    except KeyError as err:
        raise HTTPException(
            status_code=404, detail=f"Routing not found for '{media_type}'"
        ) from err


@router.put("/media/{media_type}", response_model=RoutingConfig)
def update_media_routing(media_type: str, req: UpdateRoutingRequest) -> RoutingConfig:
    """Update routing configuration for a media type.

    Assign debrid and download arr instances, set the default path,
    and record Seerr instance IDs for frontend configuration guidance.

    For media types NOT supported by Seerr (music, books, comics, audiobooks,
    adult), routing is enforced at the arr download client level — the arr
    app uses the appropriate download client (Decypharr vs qBittorrent).
    """
    if media_type not in VALID_MEDIA_TYPES:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown media type. Valid: {VALID_MEDIA_TYPES}",
        )
    if req.default_path not in ("debrid", "download", "ask"):
        raise HTTPException(
            status_code=422,
            detail="default_path must be: debrid | download | ask",
        )

    # Validate instance keys exist if provided
    with StateDB() as db:
        for inst_key, field_name in [
            (req.debrid_instance, "debrid_instance"),
            (req.download_instance, "download_instance"),
        ]:
            if inst_key:
                app = db.get_app(inst_key)
                if not app:
                    raise HTTPException(
                        status_code=404,
                        detail=f"{field_name} '{inst_key}' is not installed.",
                    )

        db._c.execute(
            """
            UPDATE request_routing SET
                debrid_instance=?, download_instance=?,
                seerr_debrid_id=?, seerr_download_id=?,
                default_path=?, notes=?,
                updated_at=unixepoch()
            WHERE media_type=?
            """,
            (
                req.debrid_instance,
                req.download_instance,
                req.seerr_debrid_id,
                req.seerr_download_id,
                req.default_path,
                req.notes,
                media_type,
            ),
        )
        # NOTE: StateDB auto-commits on __exit__ — db._c.commit() removed (Core Rule 4.4)

    return _routing_to_out(_load_routing(media_type))


@router.get("/media/{media_type}/seerr-help")
def seerr_setup_help(media_type: str) -> dict[str, Any]:
    """Return step-by-step instructions for configuring request routing in Seerr.

    For Seerr-supported types (movies/tv): instructions to set up per-user
    instance routing in Overseerr/Jellyseerr.

    For non-Seerr types (music/books/comics/audiobooks/adult): instructions
    to configure the arr download client directly for debrid or download.
    """
    if media_type not in VALID_MEDIA_TYPES:
        raise HTTPException(status_code=404, detail=f"Unknown media type: {media_type}")

    try:
        routing = _load_routing(media_type)
    except KeyError as err:
        raise HTTPException(
            status_code=404, detail=f"No routing configured for {media_type}"
        ) from err

    debrid_inst = routing.get("debrid_instance")
    download_inst = routing.get("download_instance")
    canonical = MEDIA_TYPE_MANIFEST.get(media_type, "unknown")

    if media_type in SEERR_SUPPORTED:
        arr_name = "Radarr" if media_type == "movies" else "Sonarr"
        steps = [
            f"## Routing {media_type} requests in Overseerr/Jellyseerr",
            "",
            "### Step 1 — Add both arr instances to Seerr",
            f"Settings → Services → {arr_name} → Add {arr_name}",
            f"  • Name: '{download_inst or arr_name} (Download)'",
            f"    URL: http://{download_inst or canonical}:{'7878' if media_type == 'movies' else '8989'}",
            f"  • Name: '{debrid_inst or arr_name} (Debrid)'",
            f"    URL: http://{debrid_inst or (canonical + '_debrid')}:{'7879' if media_type == 'movies' else '8990'}",
            "",
            "### Step 2 — Note the instance IDs",
            "After saving each instance, Seerr shows the instance ID in the URL:",
            "  Settings → Services → Radarr → Edit → URL will contain /settings/radarr/X",
            "Record these IDs and enter them in SLOP → Routing → Media Types.",
            "",
            "### Step 3 — Configure default instance",
            "Settings → Services → Default {arr_name} → choose your download instance.",
            "This is the fallback for users without a specific assignment.",
            "",
            "### Step 4 — Configure per-user routing",
            "Users → [select user] → Settings → {arr_name} Instance",
            "  • Debrid users → select the Debrid instance",
            "  • Standard users → select the Download instance",
            "",
            "### Step 5 — (Optional) User permissions groups",
            "Settings → Users → Default Permissions → set a default {arr_name} instance",
            "for new users based on whether you want debrid or download as the default.",
        ]
        return {
            "media_type": media_type,
            "seerr_supported": True,
            "routing": routing,
            "steps": steps,
        }
    else:
        # Non-Seerr types — configure at the arr download client level
        arr_display = {
            "music": "Lidarr",
            "books": "Readarr",
            "audiobooks": "Readarr (audiobooks mode)",
            "comics": "Mylar3",
            "adult": "Whisparr",
        }.get(media_type, canonical.title())

        steps = [
            f"## Routing {media_type} requests",
            "",
            f"Seerr does not support {media_type} request routing natively.",
            f"Configure routing at the {arr_display} download client level instead:",
            "",
            "### For debrid path:",
            f"  1. Open {arr_display} → Settings → Download Clients",
            "  2. Add Decypharr (qBittorrent type, host: decypharr, port: 8282)",
            "  3. Set Decypharr as the default download client",
            "  4. The arr will route ALL downloads through Decypharr → Real-Debrid",
            "",
            "### For download path:",
            f"  1. Open {arr_display} → Settings → Download Clients",
            "  2. Add qBittorrent (host: qbittorrent) or SABnzbd (host: sabnzbd)",
            "  3. Set as default download client",
            "",
            "### For hybrid (debrid preferred, download fallback):",
            "  1. Add both Decypharr AND qBittorrent as download clients",
            "  2. In Decypharr settings, enable 'Download uncached files' → this",
            "     falls back to local download when content is not cached on RD",
            "",
            f"### Multiple {arr_display} instances:",
            "  Install a second instance for the other path:",
            f"  POST /api/routing/instances/{canonical}",
            f'  {{"instance_key": "{canonical}_debrid", "label": "{arr_display} (Debrid)", "role": "debrid"}}',
        ]
        return {
            "media_type": media_type,
            "seerr_supported": False,
            "routing": routing,
            "steps": steps,
        }
