"""backend/api/auth_policy.py — #976 Phase A: control-plane auth policy + guard.

Closes #976's wiring gap. A0 (``backend/api/auth.py``) shipped a default-deny
token/scope check but was wired to only ~4 of ~190 routes. This module supplies
the **single router-inclusion-level dependency** (``control_plane_guard``) that the
DToC consensus (docs/CONTROL-PLANE-AUTH-PROPOSAL.md §7-§9, wf_f7e708dd-893) settled
on as the can't-forget chokepoint — wired once into ``main.py``'s ``_mount`` it
covers every current AND future mounted route by construction.

Classification (§1 / judge C5): HTTP-method default + an **endpoint-identity** override
table. The key is the endpoint function's identity (``__module__``, ``__qualname__``) —
NOT the path — so it is prefix-agnostic under main.py's dual-mount (the SAME handler
backs both ``/api/v1/x`` and ``/api/x``), with no path-normalization hole.

Migration (§3 / judge C6): a tri-state mode ``off | observe | enforce`` (StateDB
setting ``control_plane_auth_mode``, default ``off`` = zero behaviour change on upgrade):
  * ``off``     — guard is a no-op (the new router-level gate is suppressed; A0's own
                  per-route deps, where still present, keep fail-closing ACT).
  * ``observe`` — resolve scope + would-reject decision, INCREMENT a machine-readable
                  StateDB counter, but ALLOW (dry-run to prove zero false lockouts).
  * ``enforce`` — apply A0's token/scope check (ACT without a valid token → 401/403;
                  READ stays token-free for the local model).
"""

from __future__ import annotations

import enum
from collections.abc import Callable

from fastapi import Header, HTTPException, Request

from backend.api.auth import (
    Scope,
    _configured_token,
    _present_token,
    _token_matches,
)
from backend.core.logging import get_logger

log = get_logger(__name__)

_MODE_SETTING = "control_plane_auth_mode"
#: StateDB counter (machine-readable observe-mode sink, §7.1) — would-reject events.
_OBSERVE_COUNTER = "control_plane_auth_observe_would_reject"
_MUTATING = frozenset({"POST", "PUT", "DELETE", "PATCH"})


class AuthMode(enum.Enum):
    """Tri-state migration mode for the control-plane guard."""

    OFF = "off"
    OBSERVE = "observe"
    ENFORCE = "enforce"


def current_mode() -> AuthMode:
    """Resolve the live tri-state mode from StateDB; default OFF (fail-safe-open).

    OFF is the upgrade default so an existing install sees zero behaviour change.
    The Settings posture badge (§C6) renders RED while this is OFF so the inert
    state cannot silently rot (the no-phantom-owner freshness signal).
    """
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            raw = (db.get_setting(_MODE_SETTING) or "off").strip().lower()
        return AuthMode(raw)
    except Exception as exc:  # unresolved → fail SAFE-OPEN to OFF (never lock out on a DB blip)
        log.debug("auth_policy: mode read failed, defaulting OFF: %s", exc)
        return AuthMode.OFF


def method_default(method: str) -> Scope:
    """Default classification: mutating verbs → ACT, everything else → READ."""
    return Scope.ACT if method.upper() in _MUTATING else Scope.READ


def _endpoint_key(endpoint: Callable[..., object]) -> tuple[str, str]:
    """Endpoint-identity key (§C5) — prefix-agnostic under the dual-mount.

    The same handler object backs both ``/api/v1/x`` and ``/api/x`` mounts, so keying
    on (module, qualname) — its identity — needs no per-mount duplication and no path
    normalization. Encoded as strings (not the raw callable) to avoid import cycles
    with the router modules.
    """
    return (getattr(endpoint, "__module__", ""), getattr(endpoint, "__qualname__", ""))


#: Override table — DEVIATIONS from ``method_default`` ONLY, keyed by endpoint identity.
#: Seeded from docs/CONTROL-PLANE-AUTH-PROPOSAL.md Appendix A (read-like POSTs that mutate
#: nothing: validate/preview/preflight/probe/lint compute). Verified no-write in the audit.
#: Every entry is a deliberate, recorded scope decision (the §C11 requirement for routes
#: whose method-default would mis-classify them).
_OVERRIDES: dict[tuple[str, str], Scope] = {
    ("backend.api.apps", "lint_compose_yaml"): Scope.READ,
    ("backend.api.apps", "probe_health_path"): Scope.READ,
    ("backend.api.apps", "batch_preflight"): Scope.READ,
    ("backend.api.health", "llm_test"): Scope.READ,
    ("backend.api.models", "validate_model_file"): Scope.READ,
    ("backend.api.models", "preflight_download"): Scope.READ,
    ("backend.api.models", "evaluate_model"): Scope.READ,
    ("backend.api.models", "evaluate_hardware_for_model"): Scope.READ,
    ("backend.api.platform", "wizard_validate"): Scope.READ,
    ("backend.api.platform", "wizard_validate_secrets"): Scope.READ,
    # §C11 recorded decision: /api/coverage is a GET (method-default READ) but spawns the
    # ms-coverage subprocess = an exec surface. Recorded as ACT so the unauth exec path is
    # a deliberate, gated choice — never an accident of census invisibility.
    ("backend.api.main", "get_coverage_map"): Scope.ACT,
}


def scope_for_route(endpoint: Callable[..., object], method: str) -> Scope:
    """Classify a route: endpoint-identity override else the method default."""
    return _OVERRIDES.get(_endpoint_key(endpoint), method_default(method))


def _bump_observe_counter() -> None:
    """Increment the machine-readable would-reject counter (observe sink, §7.1)."""
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            cur = int(db.get_setting(_OBSERVE_COUNTER) or "0")
            db.set_setting(_OBSERVE_COUNTER, str(cur + 1))
    except Exception as exc:  # a counter blip must never break a request
        log.debug("auth_policy: observe counter bump failed: %s", exc)


def token_provisioned() -> bool:
    """True iff a control-plane token is configured (env or StateDB setting).

    Exposes only the FACT of provisioning for the posture badge (§C6) — never the
    token value (L3: the token must never be READ-readable).
    """
    return _configured_token() is not None


def observe_would_reject_count() -> int:
    """Read the machine-readable observe-mode would-reject counter (§7.1).

    Public reader for the posture badge (§C6) so the StateDB counter key stays
    encapsulated in this module (its sole owner — `_bump_observe_counter` writes it).
    Returns 0 on any read failure (a counter blip must never break the badge).
    """
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            return int(db.get_setting(_OBSERVE_COUNTER) or "0")
    except Exception as exc:
        log.debug("auth_policy: observe counter read failed: %s", exc)
        return 0


def _would_reject(scope: Scope, authorization: str | None, x_api_key: str | None) -> bool:
    """A0 default-deny predicate: True if enforce mode WOULD reject this request."""
    configured = _configured_token()
    presented = _present_token(authorization, x_api_key)
    if configured:
        return not _token_matches(presented, configured)
    # No token configured: ACT is locked (reject), READ is token-free (allow).
    return scope is Scope.ACT


async def control_plane_guard(
    request: Request,
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> None:
    """Router-inclusion-level control-plane guard (the #976 chokepoint).

    Runs post-routing, so ``request.scope['route']`` carries the resolved ``APIRoute``
    (its ``.endpoint`` identity drives the override lookup — what Starlette middleware
    cannot see). Wire ONCE into ``main.py:_mount`` (both dual-mount includes) + the
    quickstart manual includes + explicitly onto the ``@app``-level ``/api/coverage``.
    """
    route = request.scope.get("route")
    endpoint = getattr(route, "endpoint", None)
    if endpoint is None:  # unresolved route (shouldn't happen post-routing) → fail safe
        return
    scope = scope_for_route(endpoint, request.method)

    mode = current_mode()
    if mode is AuthMode.OFF:
        return
    if not _would_reject(scope, authorization, x_api_key):
        return
    # Would reject:
    if mode is AuthMode.OBSERVE:
        _bump_observe_counter()
        log.info(
            "auth_policy[observe]: WOULD reject %s %s (scope=%s)",
            request.method,
            request.url.path,
            scope.value,
        )
        return
    # ENFORCE — mirror A0's status codes.
    configured = _configured_token()
    if configured:
        raise HTTPException(status_code=401, detail="invalid or missing control-plane token")
    raise HTTPException(
        status_code=403,
        detail=(
            "control-plane act-scope is locked: set SLOP_CONTROL_PLANE_TOKEN "
            "(or the control_plane_token setting) to enable agent actions"
        ),
    )


__all__ = [
    "AuthMode",
    "control_plane_guard",
    "current_mode",
    "method_default",
    "observe_would_reject_count",
    "scope_for_route",
    "token_provisioned",
]
