"""backend/api/control_plane.py — #976 Phase-C: control-plane auth posture badge.

Read-only posture endpoint backing the Settings UI badge (DToC judge C6 — the
no-phantom-owner freshness signal). The control-plane auth feature ships default
``off`` (zero behaviour change on upgrade), so without a visible posture it would
silently rot inert. The badge renders **RED** while the plane is inert (mode=off
OR no token provisioned), **AMBER** in observe (dry-run), **green** in enforce.

Exposes only the *fact* of provisioning (a bool) — never the token value (L3: the
control-plane token must never be READ-readable). Mounted at
``/api/v1/control-plane`` and ``/api/control-plane`` via ``_mount`` in main.py; a
GET classifies as READ under the guard, so it stays token-free for the local model.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from backend.api.auth_policy import (
    AuthMode,
    current_mode,
    observe_would_reject_count,
    token_provisioned,
)


router = APIRouter()


class ControlPlanePosture(BaseModel):
    """Posture snapshot for the Settings badge (§C6)."""

    mode: str  # off | observe | enforce
    token_provisioned: bool
    posture: str  # red | amber | green
    observe_would_reject_count: int


def _posture_for(mode: AuthMode, provisioned: bool) -> str:
    """Derive the badge colour. RED dominates: an inert OR unprovisioned plane is
    never green/amber regardless of mode (judge C6)."""
    if mode is AuthMode.OFF or not provisioned:
        return "red"
    if mode is AuthMode.OBSERVE:
        return "amber"
    return "green"  # ENFORCE + provisioned


@router.get("/posture", response_model=ControlPlanePosture)
def get_posture() -> ControlPlanePosture:
    """Live control-plane auth posture (derived from StateDB + env — no stored copy)."""
    mode = current_mode()
    provisioned = token_provisioned()
    return ControlPlanePosture(
        mode=mode.value,
        token_provisioned=provisioned,
        posture=_posture_for(mode, provisioned),
        observe_would_reject_count=observe_would_reject_count(),
    )
