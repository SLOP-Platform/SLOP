"""backend/api/catalog.py

Catalog API routes.

GET /api/catalog          — all app manifests as catalog entries
GET /api/catalog/{key}    — single app manifest detail
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.core.logging import get_logger
from backend.manifests.loader import ManifestError, load_all_manifests, load_manifest

log = get_logger(__name__)
router = APIRouter()


class CatalogEntry(BaseModel):
    key: str
    display_name: str
    description: str
    category: str
    tier: int
    icon: str
    web_port: int | None
    linuxserver: bool
    tags: list[str]
    links: dict[str, str]
    has_gpu: bool
    gpu_optional: bool | None = None
    hardware_note: str | None = None
    start_grace_s: int = 60
    dependencies: dict[str, Any]
    install_prompts: list[dict[str, Any]] = []


class CatalogDetail(CatalogEntry):
    image: str
    image_tag: str
    traefik_enabled: bool
    traefik_subdomain: str
    wiring: list[dict[str, Any]]
    health_checks: list[dict[str, Any]]
    post_deploy_steps: int


@router.get("", response_model=dict[str, list[CatalogEntry]])
def get_catalog() -> dict[str, list[CatalogEntry]]:
    """Return all catalog apps grouped by category."""
    manifests = load_all_manifests()
    grouped: dict[str, list[CatalogEntry]] = {}
    for _, m in sorted(manifests.items()):
        entry = m.to_catalog_entry()
        cat = entry["category"]
        grouped.setdefault(cat, []).append(CatalogEntry(**entry))
    return grouped


@router.get("/{key}", response_model=CatalogDetail)
def get_catalog_entry(key: str) -> CatalogDetail:
    """Return full manifest detail for a single app."""
    try:
        m = load_manifest(key)
    except KeyError as err:
        raise HTTPException(status_code=404, detail=f"No app '{key}' in catalog.") from err
    except ManifestError as e:
        raise HTTPException(status_code=500, detail=f"Manifest error: {e.message}") from e

    base = m.to_catalog_entry()
    return CatalogDetail(
        **base,
        image=m.image,
        image_tag=m.image_tag,
        traefik_enabled=m.traefik_enabled,
        traefik_subdomain=m.traefik_sub(),
        wiring=[
            {
                "wire_type": w.wire_type,
                "peer": w.peer,
                "direction": w.direction,
                "optional": w.optional,
                "description": w.description,
            }
            for w in m.wiring
        ],
        health_checks=[
            {
                "name": h.name,
                "type": h.check_type,
                "path": h.path,
                "interval": h.interval,
            }
            for h in m.health_checks
        ],
        post_deploy_steps=len(m.post_deploy),
    )
