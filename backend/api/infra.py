"""backend/api/infra.py

Infrastructure slot API routes.

GET  /api/infra/slots                      — all slot statuses
GET  /api/infra/slots/{slot}               — single slot detail
GET  /api/infra/providers                  — all registered providers
GET  /api/infra/providers/{slot}           — providers available for a slot
POST /api/infra/{slot}/deploy              — deploy a provider into a slot
POST /api/infra/{slot}/swap                — swap from one provider to another (single-provider slots)
POST /api/infra/{slot}/verify              — verify current provider is working
POST /api/infra/{slot}/remove              — remove current provider from slot
POST /api/infra/tunnel/{provider}/verify   — verify a specific tunnel provider
POST /api/infra/tunnel/{provider}/remove   — remove a specific tunnel provider
GET  /api/infra/migrations                 — recent migration history

Tunnel is a multi-provider slot: Cloudflare, Tailscale, and Headscale can all
run simultaneously. All other slots are single-provider.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from backend.core.error_detail import safe_detail
from backend.core.logging import get_logger
from backend.core.state import StateDB
import backend.infra.providers  # noqa: F401 — triggers @register
from backend.api.infra_schemas import PROVIDER_CONFIG_SCHEMAS
from backend.infra.slots import deployable_slots
from backend.infra.registry import (
    get_provider,
    list_providers,
    swap_slot,
)

log = get_logger(__name__)
router = APIRouter()

MULTI_PROVIDER_SLOTS = {"tunnel"}


# ── Models ────────────────────────────────────────────────────────────────


class SlotOut(BaseModel):
    slot: str
    provider: str | None  # primary provider (single-provider slots)
    providers: list[dict[str, Any]] = []  # all active providers (tunnel)
    status: str
    config: dict[str, Any]
    deployed_at: int | None


class ProviderOut(BaseModel):
    slot: str
    key: str
    display_name: str


class DeployRequest(BaseModel):
    provider: str
    config: dict[str, Any] = {}


class SwapRequest(BaseModel):
    to_provider: str
    config: dict[str, Any] = {}


class SwapStepOut(BaseModel):
    name: str
    status: str
    message: str
    detail: str = ""


class SwapOut(BaseModel):
    ok: bool
    slot: str
    from_provider: str
    to_provider: str
    steps: list[SwapStepOut]
    rolled_back: bool
    error: str = ""


class VerifyOut(BaseModel):
    ok: bool
    message: str
    detail: str = ""


# ── Routes ────────────────────────────────────────────────────────────────


@router.get("/slots", response_model=list[SlotOut])
def get_slots() -> list[SlotOut]:
    """Return the current state of all deployable infrastructure slots."""
    with StateDB() as db:
        slots = db.get_all_slots()
        tunnel_providers = db.get_tunnel_providers()

    out = []
    for s in slots:
        if s.slot == "tunnel":
            active = [p for p in tunnel_providers if p["status"] == "active"]
            out.append(
                SlotOut(
                    slot=s.slot,
                    provider=active[0]["provider"] if active else None,
                    providers=tunnel_providers,
                    status="active" if active else "empty",
                    config=active[0]["config"] if active else {},
                    deployed_at=active[0]["deployed_at"] if active else None,
                )
            )
        else:
            out.append(
                SlotOut(
                    slot=s.slot,
                    provider=s.provider,
                    providers=[],
                    status=s.status,
                    config=s.config,
                    deployed_at=s.deployed_at,
                )
            )
    return out


@router.get("/slots/{slot}", response_model=SlotOut)
def get_slot(slot: str) -> SlotOut:
    """Return the current state of a single infrastructure slot."""
    if slot == "tunnel":
        with StateDB() as db:
            tunnel_providers = db.get_tunnel_providers()
        active = [p for p in tunnel_providers if p["status"] == "active"]
        return SlotOut(
            slot="tunnel",
            provider=active[0]["provider"] if active else None,
            providers=tunnel_providers,
            status="active" if active else "empty",
            config=active[0]["config"] if active else {},
            deployed_at=active[0]["deployed_at"] if active else None,
        )
    try:
        with StateDB() as db:
            s = db.get_slot(slot)
    except Exception as err:
        raise HTTPException(status_code=404, detail=f"Slot '{slot}' not found.") from err
    return SlotOut(
        slot=s.slot,
        provider=s.provider,
        providers=[],
        status=s.status,
        config=s.config,
        deployed_at=s.deployed_at,
    )


@router.get("/providers", response_model=list[ProviderOut])
def get_all_providers() -> list[ProviderOut]:
    """Return all registered infrastructure providers."""
    return [ProviderOut(**p) for p in list_providers()]


@router.get("/providers/{slot}", response_model=list[ProviderOut])
def get_slot_providers(slot: str) -> list[ProviderOut]:
    """Return providers available for a specific slot."""
    return [ProviderOut(**p) for p in list_providers(slot=slot)]


@router.post("/{slot}/deploy", response_model=VerifyOut)
def deploy_provider(slot: str, req: DeployRequest) -> VerifyOut:
    """Deploy a provider into a slot.

    For multi-provider slots (tunnel): multiple providers can be active simultaneously.
    For single-provider slots: rejects if a provider is already active (use /swap).
    """
    if slot == "tunnel":
        # Multi-provider: check only if THIS specific provider is already active
        with StateDB() as db:
            existing = db.get_tunnel_provider(req.provider)
        if existing and existing["status"] == "active":
            raise HTTPException(
                status_code=409,
                detail=f"Tunnel provider '{req.provider}' is already active.",
            )
    else:
        # All deployable single-provider slots (tunnel is multi-provider, handled above).
        VALID_SLOTS = set(deployable_slots()) - {"tunnel"}
        if slot not in VALID_SLOTS:
            raise HTTPException(
                status_code=404,
                detail=f"Unknown infrastructure slot '{slot}'. "
                f"Valid slots: {', '.join(sorted(VALID_SLOTS))}.",
            )
        with StateDB() as db:
            current = db.get_slot(slot)
        if current.status == "active":
            raise HTTPException(
                status_code=409,
                detail=f"Slot '{slot}' already has '{current.provider}' active. "
                f"Use /swap to change providers.",
            )

    try:
        provider = get_provider(slot, req.provider)
    except KeyError as e:
        raise HTTPException(
            status_code=404,
            detail=safe_detail(e, f"No registered provider for slot '{slot}'.", log=log),
        ) from e

    result = provider.deploy(req.config)
    return VerifyOut(ok=result.ok, message=result.message, detail=result.detail)


@router.post("/{slot}/swap", response_model=SwapOut)
def swap_provider(slot: str, req: SwapRequest) -> SwapOut:
    """Swap the active provider in a single-provider slot.

    Not applicable to the tunnel slot (use /deploy to add, /tunnel/{provider}/remove to remove).
    """
    if slot == "tunnel":
        raise HTTPException(
            status_code=400,
            detail="Tunnel is a multi-provider slot. Use /deploy to add a provider "
            "and /tunnel/{provider}/remove to remove one.",
        )

    with StateDB() as db:
        current = db.get_slot(slot)

    if current.status != "active" or not current.provider:
        raise HTTPException(
            status_code=409,
            detail=f"Slot '{slot}' has no active provider to swap from. "
            f"Use /deploy to install the first provider.",
        )

    if current.provider == req.to_provider:
        raise HTTPException(
            status_code=400,
            detail=f"Slot '{slot}' is already using '{req.to_provider}'.",
        )

    result = swap_slot(slot, current.provider, req.to_provider, req.config)
    return SwapOut(
        ok=result.ok,
        slot=result.slot,
        from_provider=result.from_provider,
        to_provider=result.to_provider,
        steps=[SwapStepOut(**s.__dict__) for s in result.steps],
        rolled_back=result.rolled_back,
        error=result.error,
    )


@router.post("/{slot}/verify", response_model=VerifyOut)
def verify_provider(slot: str, provider_key: str | None = None) -> VerifyOut:
    """Verify the current provider in a slot is working.

    For tunnel slot, verifies all active providers or a specific one via ?provider_key=.
    """
    _valid_slots = set(deployable_slots())
    if slot not in _valid_slots:
        raise HTTPException(
            status_code=404, detail=f"Unknown slot '{slot}'. Valid: {sorted(_valid_slots)}"
        )
    if slot == "tunnel":
        with StateDB() as db:
            providers = db.get_tunnel_providers()
        active = [p for p in providers if p["status"] == "active"]
        if not active:
            raise HTTPException(status_code=404, detail="No active tunnel providers.")
        target = (
            active[0]
            if not provider_key
            else next((p for p in active if p["provider"] == provider_key), None)
        )
        if not target:
            raise HTTPException(
                status_code=404, detail=f"Tunnel provider '{provider_key}' not active."
            )
        try:
            provider = get_provider("tunnel", target["provider"])
        except KeyError as e:
            raise HTTPException(
                status_code=404,
                detail=safe_detail(e, f"No registered provider for slot '{slot}'.", log=log),
            ) from e
        result = provider.verify()
        return VerifyOut(ok=result.ok, message=result.message, detail=result.detail)

    with StateDB() as db:
        current = db.get_slot(slot)
    if not current.provider:
        raise HTTPException(status_code=404, detail=f"No provider deployed in slot '{slot}'.")
    try:
        provider = get_provider(slot, current.provider)
    except KeyError as e:
        raise HTTPException(
            status_code=404,
            detail=safe_detail(e, f"No registered provider for slot '{slot}'.", log=log),
        ) from e
    result = provider.verify()
    return VerifyOut(ok=result.ok, message=result.message, detail=result.detail)


@router.post("/{slot}/remove", response_model=VerifyOut)
def remove_provider(slot: str, provider_key: str | None = None) -> VerifyOut:
    """Remove the current provider from a slot.

    For tunnel slot, requires provider_key query param to specify which tunnel to remove.
    """
    _valid_slots = set(deployable_slots())
    if slot not in _valid_slots:
        raise HTTPException(
            status_code=404, detail=f"Unknown slot '{slot}'. Valid: {sorted(_valid_slots)}"
        )
    if slot == "tunnel":
        if not provider_key:
            raise HTTPException(
                status_code=400,
                detail="Tunnel is multi-provider. Specify ?provider_key=cloudflared (etc).",
            )
        with StateDB() as db:
            existing = db.get_tunnel_provider(provider_key)
        if not existing or existing["status"] != "active":
            raise HTTPException(
                status_code=404, detail=f"Tunnel provider '{provider_key}' not active."
            )
        try:
            provider = get_provider("tunnel", provider_key)
        except KeyError as e:
            raise HTTPException(
                status_code=404,
                detail=safe_detail(e, f"No registered provider for slot '{slot}'.", log=log),
            ) from e
        result = provider.remove()
        return VerifyOut(ok=result.ok, message=result.message, detail=result.detail)

    with StateDB() as db:
        current = db.get_slot(slot)
    if not current.provider:
        raise HTTPException(status_code=404, detail=f"No provider deployed in slot '{slot}'.")
    try:
        provider = get_provider(slot, current.provider)
    except KeyError as e:
        raise HTTPException(
            status_code=404,
            detail=safe_detail(e, f"No registered provider for slot '{slot}'.", log=log),
        ) from e
    result = provider.remove()
    return VerifyOut(ok=result.ok, message=result.message, detail=result.detail)


@router.get("/migrations")
def get_migrations() -> list[dict[str, Any]]:
    """Return recent infrastructure migration history."""
    with StateDB() as db:
        rows = db.execute(
            """SELECT slot, from_provider, to_provider, status,
                      steps_completed, steps_total, current_step,
                      started_at, completed_at
               FROM infra_migrations
               ORDER BY started_at DESC
               LIMIT 20"""
        ).fetchall()
    return [dict(r) for r in rows]


@router.get("/providers/{slot}/schema", tags=["Infrastructure"])
def get_provider_schema(slot: str) -> list[dict[str, Any]]:
    """Return config field schemas for all providers in a slot."""
    providers = list_providers(slot)
    return [
        {
            "key": p["key"],
            "display_name": p["display_name"],
            "slot": p["slot"],
            "fields": PROVIDER_CONFIG_SCHEMAS.get(p["key"], []),
        }
        for p in providers
    ]
