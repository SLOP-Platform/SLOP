"""backend/api/auth.py — A0: minimal default-deny token + scope auth (partial #976).

Scope (A0, PARTIAL — does NOT close #976):
  * A bearer/``X-API-Key`` token gate for the AGENT CONTROL PLANE (the act-vs-ask
    surface: chat-driven actions, the future MCP adapter, kill-switch). It is a
    FastAPI dependency, ``require_scope(...)``, that callers attach to a route.
  * **Default-deny.** When the control-plane token is configured, a request
    without a matching token is rejected (401). When NO token is configured, the
    control plane is treated as locked down: act-scope requests are DENIED (403)
    rather than silently allowed — the fail-closed direction. Read-only scope is
    permitted token-free for the existing local-only deployment model (Traefik
    fronts the rest), matching today's behaviour without weakening it.

DEFERRED to the #976 follow-up (NOT in A0): SameSite session cookies, the full
apply-endpoint retrofit across every mutating route, and per-user identity. A0 is
recorded as a sub-deliverable of #976, never its closure.

Token source (first hit wins):
  1. env ``SLOP_CONTROL_PLANE_TOKEN``
  2. settings key ``control_plane_token`` (StateDB)
"""

from __future__ import annotations

import enum
import hmac
import os

from fastapi import Depends, Header, HTTPException

from backend.core.logging import get_logger

log = get_logger(__name__)

_ENV_TOKEN = "SLOP_CONTROL_PLANE_TOKEN"  # noqa: S105 - env-var / setting NAME, not a secret value
_SETTING_TOKEN = "control_plane_token"  # noqa: S105 - env-var / setting NAME, not a secret value


class Scope(enum.Enum):
    """Control-plane scopes. ``READ`` is observe-only; ``ACT`` mutates."""

    READ = "read"
    ACT = "act"


def _configured_token() -> str | None:
    """Resolve the control-plane token from env then settings; None if unset."""
    env = os.environ.get(_ENV_TOKEN)
    if env:
        return env
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            return db.get_setting(_SETTING_TOKEN)
    except Exception as exc:  # fail-closed: treat unresolved as "no token"
        log.debug("auth: settings token read failed: %s", exc)
        return None


def _present_token(authorization: str | None, x_api_key: str | None) -> str | None:
    """Extract a presented token from either header form."""
    if x_api_key:
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        return authorization[len("bearer ") :].strip()
    return None


def _token_matches(presented: str | None, configured: str) -> bool:
    """Constant-time token comparison; False for a missing presented token."""
    if not presented:
        return False
    return hmac.compare_digest(presented, configured)


def require_scope(scope: Scope):  # type: ignore[no-untyped-def]
    """Return a FastAPI dependency enforcing *scope* on the control plane.

    Behaviour (default-deny):
      * Token configured + presented + matches  → allow.
      * Token configured + (absent or mismatch) → 401.
      * No token configured + scope is ACT       → 403 (locked-down: never allow a
        mutation against an unconfigured control plane).
      * No token configured + scope is READ      → allow (local read model).
    """

    def _dep(
        authorization: str | None = Header(default=None),
        x_api_key: str | None = Header(default=None, alias="X-API-Key"),
    ) -> Scope:
        configured = _configured_token()
        presented = _present_token(authorization, x_api_key)

        if configured:
            if not _token_matches(presented, configured):
                raise HTTPException(
                    status_code=401, detail="invalid or missing control-plane token"
                )
            return scope

        # No token configured.
        if scope is Scope.ACT:
            raise HTTPException(
                status_code=403,
                detail=(
                    "control-plane act-scope is locked: set SLOP_CONTROL_PLANE_TOKEN "
                    "(or the control_plane_token setting) to enable agent actions"
                ),
            )
        return scope

    return _dep


# Convenience pre-bound dependencies for route authors.
require_read = Depends(require_scope(Scope.READ))
require_act = Depends(require_scope(Scope.ACT))


# Audit-actor labels (NOT access decisions — see resolve_actor).
ACTOR_CONTROL_PLANE = "control-plane"
ACTOR_LOCAL = "local"


def resolve_actor(authorization: str | None, x_api_key: str | None) -> str:
    """Best-effort principal label for the audit log (#978) — NOT an access decision.

    Returns ``ACTOR_CONTROL_PLANE`` when the request presents a token matching the
    configured control-plane token, else ``ACTOR_LOCAL`` (the unauthenticated
    local-deployment actor, the prior hardcoded default). The granularity is binary
    by design: A0 has no per-user identity — finer per-principal attribution is
    deferred to the #976 SameSite-session follow-up. This is deliberately decoupled
    from ``require_scope`` so the audit middleware can attribute EVERY mutating
    request (most routes are not yet wired to the dependency), reusing the same
    constant-time token check (no access control, no secret logged — only the label).
    """
    configured = _configured_token()
    if configured and _token_matches(_present_token(authorization, x_api_key), configured):
        return ACTOR_CONTROL_PLANE
    return ACTOR_LOCAL


__all__ = [
    "ACTOR_CONTROL_PLANE",
    "ACTOR_LOCAL",
    "Scope",
    "require_act",
    "require_read",
    "require_scope",
    "resolve_actor",
]
