"""backend/api/updates.py

Container update status API — SLOP polls Docker registries directly via the
Docker daemon's inspect_distribution endpoint. No external update service required.

GET  /api/updates/status       — compare running image digests to registry
PUT  /api/updates/preferences  — store per-container notify/pin preferences
GET  /api/updates/preferences  — return stored preferences
"""

from __future__ import annotations
from typing import Any
import json
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from backend.core.docker_client import client, DockerError
from backend.core.logging import get_logger
from backend.core.state import StateDB

log = get_logger(__name__)
router = APIRouter()

_PREFS_KEY = "update_preferences"
# SLOP's own container is pinned by default — auto-updating it can restart
# the manager out from under a running operation.
SELF_CONTAINER_KEY = "slop"


def _load_prefs(db: StateDB) -> dict[str, dict[str, bool]]:
    raw = db.get_setting(_PREFS_KEY)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (ValueError, TypeError):
        return {}


def _save_prefs(db: StateDB, prefs: dict[str, dict[str, bool]]) -> None:
    db.set_setting(_PREFS_KEY, json.dumps(prefs))


def _default_pref(container_key: str) -> dict[str, bool]:
    return {
        "notify_only": True,
        "pinned": container_key == SELF_CONTAINER_KEY,
    }


def _poll_containers(prefs: dict[str, dict[str, bool]]) -> list[dict[str, Any]]:
    """Compare each running container's local image digest to the registry digest.

    Uses Docker daemon's inspect_distribution which handles registry auth
    transparently. When the registry is unreachable or returns an error,
    treat the container as up-to-date (INDETERMINATE) — never emit a false
    update_available=True based on a failed registry call.
    """
    dc = client()
    try:
        containers = dc.containers.list()
    except Exception as e:
        log.info("Could not list containers: %s", e)
        return []

    result: list[dict[str, Any]] = []
    for c in containers:
        attrs = c.attrs or {}
        name = c.name
        image_ref = (attrs.get("Config") or {}).get("Image", "")
        local_digest = attrs.get("Image", "")

        update_available = False
        registry_digest: str | None = None
        if image_ref:
            try:
                dist = dc.api.inspect_distribution(image_ref)
                registry_digest = (dist.get("Descriptor") or {}).get("digest", "")
                if registry_digest and local_digest:
                    update_available = registry_digest != local_digest
            except Exception as e:
                log.info("Registry check INDETERMINATE for %s: %s", image_ref, e)

        pref = prefs.get(name, _default_pref(name))
        result.append(
            {
                "container_key": name,
                "name": name,
                "current_image": image_ref,
                "available_update": registry_digest if update_available else None,
                "update_available": update_available,
                "notify_only": pref.get("notify_only", True),
                "pinned": pref.get("pinned", name == SELF_CONTAINER_KEY),
                "is_self": name == SELF_CONTAINER_KEY,
            }
        )
    return result


@router.get("/status")
def get_update_status() -> Any:
    """Return update status for all running containers.

    Compares each container's running image digest to the current registry
    digest. Degrades gracefully: Docker unreachable → 503; individual
    registry failures → container shown as up-to-date.
    """
    with StateDB() as db:
        prefs = _load_prefs(db)

    try:
        containers = _poll_containers(prefs)
    except DockerError as e:
        log.info("Docker unreachable: %s", e)
        raise HTTPException(
            status_code=503,
            detail={
                "error": "docker_unreachable",
                "message": ("Docker is not reachable. Container update status is unavailable."),
                "containers": [],
            },
        ) from e

    return {"containers": containers}


class ContainerPref(BaseModel):
    notify_only: bool = True
    pinned: bool = False


class PreferencesPayload(BaseModel):
    preferences: dict[str, ContainerPref]


@router.put("/preferences")
def update_preferences(payload: PreferencesPayload) -> dict[str, Any]:
    """Store per-container update preferences in the StateDB kv store."""
    with StateDB() as db:
        prefs = _load_prefs(db)
        for key, pref in payload.preferences.items():
            prefs[key] = {"notify_only": pref.notify_only, "pinned": pref.pinned}
        _save_prefs(db, prefs)
    log.info("Update preferences saved for %d container(s)", len(payload.preferences))
    return {"ok": True, "preferences": prefs}


@router.get("/preferences")
def get_preferences() -> dict[str, Any]:
    """Return the stored per-container update preferences."""
    with StateDB() as db:
        prefs = _load_prefs(db)
    return {"preferences": prefs}
