"""backend/api/registry.py

Catalog registry — community manifest discovery and pull.

GET  /api/catalog/registry              — list known registry entries
POST /api/catalog/registry/sync        — fetch latest from remote registry URL
GET  /api/catalog/registry/{key}       — single registry entry
POST /api/catalog/registry/{key}/pull  — download manifest to local catalog
GET  /api/catalog/custom               — list locally installed custom manifests
DELETE /api/catalog/custom/{key}       — remove a custom manifest
"""

from __future__ import annotations

from typing import Any

import json
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request

from backend.api.rate_limit import limiter
from pydantic import BaseModel

from backend.core.error_detail import safe_detail
from backend.core.logging import get_logger
from backend.core.path_guard import PathNotAllowed, safe_component
from backend.core.state import StateDB
from backend.core.url_guard import UrlNotAllowed, assert_not_metadata_url
from backend.core.url_guard_httpx import pinned_async_client
from backend.manifests.loader import (
    ManifestError,
    clear_cache,
    load_all_manifests,
    parse_manifest as _parse_manifest,
)

log = get_logger(__name__)
router = APIRouter()

CUSTOM_CATALOG_DIR = Path("catalog/community")
REGISTRY_TIMEOUT = 10  # seconds


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------


class RegistryEntry(BaseModel):
    key: str
    display_name: str
    description: str | None
    category: str | None
    icon: str
    tier: int
    service_type: str
    tags: list[str]
    source_url: str
    author: str
    verified: bool
    installed: bool


class SyncResult(BaseModel):
    ok: bool
    added: int
    updated: int
    total: int
    registry_url: str
    error: str | None = None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_registry_url() -> str:
    try:
        with StateDB() as db:
            url = db.get_setting("registry_url")
        return (
            url or "https://raw.githubusercontent.com/SLOP-Platform/SLOP/main/catalog/registry.json"
        )
    except Exception:
        return "https://raw.githubusercontent.com/SLOP-Platform/SLOP/main/catalog/registry.json"


def _load_db_registry() -> list[dict[str, Any]]:
    with StateDB() as db:
        rows = db._c.execute(
            "SELECT * FROM manifest_registry ORDER BY category, display_name"
        ).fetchall()
    cols = [
        "id",
        "key",
        "display_name",
        "description",
        "category",
        "icon",
        "source_url",
        "author",
        "verified",
        "installed",
        "registry_url",
        "fetched_at",
        "updated_at",
    ]
    return [dict(zip(cols, r, strict=False)) for r in rows]


def _installed_keys() -> set[str]:
    try:
        manifests = load_all_manifests()
        return set(manifests.keys())
    except Exception:
        return set()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("", response_model=list[RegistryEntry])
def list_registry() -> list[RegistryEntry]:
    """List all known registry entries (last synced data).

    Run POST /sync first to populate from the remote registry.
    """
    installed = _installed_keys()
    rows = _load_db_registry()

    if not rows:
        # Return built-in catalog as registry entries on first load
        try:
            manifests = load_all_manifests()
            return [
                RegistryEntry(
                    key=m.key,
                    display_name=m.display_name,
                    description=m.description,
                    category=m.category,
                    icon=m.icon,
                    tier=m.tier,
                    service_type=m.service_type,
                    tags=m.tags[:5],
                    source_url=f"https://raw.githubusercontent.com/SLOP-Platform/SLOP/main/catalog/apps/{m.key}.yaml",
                    author="official",
                    verified=True,
                    installed=True,  # built-in = always installed
                )
                for m in manifests.values()
            ]
        except Exception:
            return []

    return [
        RegistryEntry(
            key=r["key"],
            display_name=r["display_name"],
            description=r.get("description"),
            category=r.get("category"),
            icon=r.get("icon", "📦"),
            tier=r.get("tier", 2),
            service_type=r.get("service_type", "management"),
            tags=json.loads(r.get("tags", "[]")) if r.get("tags") else [],
            source_url=r["source_url"],
            author=r.get("author", "community"),
            verified=bool(r.get("verified", 0)),
            installed=r["key"] in installed,
        )
        for r in rows
    ]


@router.post("/sync", response_model=SyncResult)
@limiter.limit("10/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped; external registry fetch (#1205 external-fetch tier)
async def sync_registry(request: Request) -> SyncResult:
    """Fetch the latest registry from the remote URL and update the DB.

    The registry is a JSON file listing available manifests with their
    source URLs. After syncing, each entry is available via /registry/{key}
    and can be pulled into the local catalog via /registry/{key}/pull.
    """
    registry_url = _get_registry_url()
    added = 0
    updated = 0

    # SSRF floor (#1193): registry_url is operator-settable, so a mis-set/hostile value
    # could aim this server-side fetch at a cloud-metadata (169.254.169.254) or
    # link-local endpoint. Block ONLY that policy-free always-deny set — a self-hosted
    # private-LAN registry (192.168.x / 10.x) stays allowed (the broader policy is #1193
    # DToC). Literal-only here (operator-config threat model; httpx resolves at connect).
    try:
        assert_not_metadata_url(registry_url, resolve_dns=False)
    except UrlNotAllowed as e:
        log.warning("registry sync blocked (SSRF floor): %s", e)
        return SyncResult(
            ok=False,
            added=0,
            updated=0,
            total=0,
            registry_url=registry_url,
            error="Registry URL points at a disallowed address (cloud-metadata/link-local).",
        )

    try:
        async with pinned_async_client(timeout=REGISTRY_TIMEOUT) as client:
            resp = await client.get(registry_url)
            resp.raise_for_status()
            data = resp.json()
    except httpx.TimeoutException:
        return SyncResult(
            ok=False,
            added=0,
            updated=0,
            total=0,
            registry_url=registry_url,
            error="Registry URL timed out. Check network and try again.",
        )
    except Exception as e:
        return SyncResult(
            ok=False,
            added=0,
            updated=0,
            total=0,
            registry_url=registry_url,
            error=safe_detail(e, "Could not fetch registry.", log=log),
        )

    manifests = data.get("manifests", [])

    with StateDB() as db:
        for entry in manifests:
            key = entry.get("key", "")
            if not key:
                continue
            existing = db._c.execute(
                "SELECT id FROM manifest_registry WHERE key=?", (key,)
            ).fetchone()

            if existing:
                db._c.execute(
                    """UPDATE manifest_registry SET
                        display_name=?, description=?, category=?, icon=?,
                        source_url=?, author=?, verified=?, registry_url=?,
                        updated_at=unixepoch()
                       WHERE key=?""",
                    (
                        entry.get("display_name", key),
                        entry.get("description", ""),
                        entry.get("category", "tools"),
                        entry.get("icon", "📦"),
                        entry.get("source_url", ""),
                        entry.get("author", "community"),
                        1 if entry.get("verified") else 0,
                        registry_url,
                        key,
                    ),
                )
                updated += 1
            else:
                db._c.execute(
                    """INSERT INTO manifest_registry
                        (key, display_name, description, category, icon,
                         source_url, author, verified, registry_url, fetched_at)
                       VALUES (?,?,?,?,?,?,?,?,?,unixepoch())""",
                    (
                        key,
                        entry.get("display_name", key),
                        entry.get("description", ""),
                        entry.get("category", "tools"),
                        entry.get("icon", "📦"),
                        entry.get("source_url", ""),
                        entry.get("author", "community"),
                        1 if entry.get("verified") else 0,
                        registry_url,
                    ),
                )
                added += 1
        # NOTE: StateDB auto-commits on __exit__ — db._c.commit() removed (Core Rule 4.4)
        db.set_setting("registry_last_sync", str(int(time.time())))

    log.info("Registry sync: %d added, %d updated from %s", added, updated, registry_url)
    return SyncResult(
        ok=True,
        added=added,
        updated=updated,
        total=len(manifests),
        registry_url=registry_url,
    )


@router.get("/{key}", response_model=RegistryEntry)
def get_registry_entry(key: str) -> RegistryEntry:
    """Get a single registry entry by key."""
    installed = _installed_keys()
    with StateDB() as db:
        row = db._c.execute("SELECT * FROM manifest_registry WHERE key=?", (key,)).fetchone()

    if not row:
        # Fall back to built-in catalog
        try:
            manifests = load_all_manifests()
            if key in manifests:
                m = manifests[key]
                return RegistryEntry(
                    key=m.key,
                    display_name=m.display_name,
                    description=m.description,
                    category=m.category,
                    icon=m.icon,
                    tier=m.tier,
                    service_type=m.service_type,
                    tags=m.tags[:5],
                    author="official",
                    verified=True,
                    source_url=f"https://raw.githubusercontent.com/SLOP-Platform/SLOP/main/catalog/apps/{key}.yaml",
                    installed=True,
                )
        except Exception:  # noqa: S110  # best-effort local manifest fallback; raise 404 below if unavailable
            pass
        raise HTTPException(
            status_code=404, detail=f"Registry entry '{key}' not found. Run /sync first."
        )

    cols = [
        "id",
        "key",
        "display_name",
        "description",
        "category",
        "icon",
        "source_url",
        "author",
        "verified",
        "installed",
        "registry_url",
        "fetched_at",
        "updated_at",
    ]
    r = dict(zip(cols, row, strict=False))
    return RegistryEntry(
        key=r["key"],
        display_name=r["display_name"],
        description=r.get("description"),
        category=r.get("category"),
        icon=r.get("icon", "📦"),
        tier=2,
        service_type="management",
        tags=[],
        source_url=r["source_url"],
        author=r.get("author", "community"),
        verified=bool(r.get("verified", 0)),
        installed=r["key"] in installed,
    )


@router.post("/{key}/pull")
@limiter.limit("10/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped; external manifest fetch (#1205 external-fetch tier)
async def pull_manifest(request: Request, key: str) -> dict[str, Any]:
    """Download a manifest from the registry into the local custom catalog.

    After pulling, the app appears in the catalog and can be installed
    via the standard POST /api/apps/{key}/install endpoint.
    """
    # Reject path-traversal in the key before it reaches any filesystem path.
    try:
        key = safe_component(key, field="key")
    except PathNotAllowed as e:
        raise HTTPException(status_code=400, detail=safe_detail(e, "Invalid key.", log=log)) from e

    # Get the source URL
    with StateDB() as db:
        row = db._c.execute(
            "SELECT source_url FROM manifest_registry WHERE key=?", (key,)
        ).fetchone()

    if not row:
        raise HTTPException(
            status_code=404, detail=f"Registry entry '{key}' not found. Run /sync first."
        )

    source_url = row[0]
    dest_path = CUSTOM_CATALOG_DIR / f"{key}.yaml"

    try:
        import tempfile as _tempfile
        import os as _os

        CUSTOM_CATALOG_DIR.mkdir(parents=True, exist_ok=True)
        async with pinned_async_client(timeout=REGISTRY_TIMEOUT) as client:
            resp = await client.get(source_url)
            resp.raise_for_status()
            raw_text = resp.text
        # Validate through the manifest parser before committing to disk.
        # Write to a tempfile, parse, then move to destination on success.
        tmp_fd, tmp_name = _tempfile.mkstemp(
            suffix=".yaml", prefix=f"registry_{key}_", dir=CUSTOM_CATALOG_DIR
        )
        try:
            with _os.fdopen(tmp_fd, "w", encoding="utf-8") as _f:
                _f.write(raw_text)
            try:
                _parse_manifest(Path(tmp_name))
            except ManifestError as _me:
                raise HTTPException(
                    status_code=422,
                    detail=f"Registry manifest failed validation: {_me}",
                ) from _me
            _os.replace(tmp_name, dest_path)
        except HTTPException:
            raise
        except Exception:
            raise
        finally:
            # Clean up tempfile if it still exists (not yet replaced)
            if _os.path.exists(tmp_name):
                _os.unlink(tmp_name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=safe_detail(e, "Could not download manifest.", log=log)
        ) from e

    # Mark as installed in registry DB
    with StateDB() as db:
        db._c.execute("UPDATE manifest_registry SET installed=1 WHERE key=?", (key,))
        # NOTE: StateDB auto-commits on __exit__ — db._c.commit() removed (Core Rule 4.4)

    # Clear loader cache so the new manifest is visible immediately
    clear_cache()

    return {
        "ok": True,
        "key": key,
        "saved_to": str(dest_path),
        "message": f"Manifest '{key}' pulled. Install via POST /api/apps/{key}/install.",
    }


@router.get("/custom/list")
def list_custom_manifests() -> list[dict[str, Any]]:
    """List locally installed custom manifests from the community catalog."""
    CUSTOM_CATALOG_DIR.mkdir(parents=True, exist_ok=True)
    entries = []
    for yaml_path in sorted(CUSTOM_CATALOG_DIR.glob("*.yaml")):
        try:
            from backend.manifests.loader import parse_manifest

            m = parse_manifest(yaml_path)
            entries.append(
                {
                    "key": m.key,
                    "display_name": m.display_name,
                    "category": m.category,
                    "icon": m.icon,
                    "source": "custom",
                    "path": str(yaml_path),
                }
            )
        except Exception as e:
            entries.append(
                {
                    "key": yaml_path.stem,
                    "display_name": yaml_path.stem,
                    "error": safe_detail(e, "Could not load manifest.", log=log),
                    "path": str(yaml_path),
                }
            )
    return entries


@router.delete("/custom/{key}")
def remove_custom_manifest(key: str) -> dict[str, Any]:
    """Remove a custom manifest from the local catalog."""
    try:
        key = safe_component(key, field="key")
    except PathNotAllowed as e:
        raise HTTPException(status_code=400, detail=safe_detail(e, "Invalid key.", log=log)) from e
    path = CUSTOM_CATALOG_DIR / f"{key}.yaml"
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Custom manifest '{key}' not found.")
    path.unlink()
    clear_cache()
    with StateDB() as db:
        db._c.execute("UPDATE manifest_registry SET installed=0 WHERE key=?", (key,))
        # NOTE: StateDB auto-commits on __exit__ — db._c.commit() removed (Core Rule 4.4)
    return {"ok": True, "key": key, "message": "Custom manifest removed."}
