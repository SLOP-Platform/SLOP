"""#1044 — declarative confirm-token marker for state-mutating routes.

A confirm-token contract (e.g. ``POST /api/platform/reset`` requires
``?confirm=RESET_PLATFORM``; ``/reset/full`` requires a *different* token
``DESTROY_ALL_DATA``) is enforced in the handler BODY
(``if confirm != "RESET_PLATFORM": raise 400``), so it cannot be read from the
route signature or the param default. That made the caller->route->token contract
UNCAPTURED — a frontend caller that omits the token silently 400s (the CL-01 live
"factory reset that wipes nothing" bug).

This decorator records the required token as an attribute on the endpoint so
``tests/route_contract.derive_route_contract`` can DERIVE the contract from the
live ``app.routes`` table — no hand-maintained route/token list to drift (#1044).

Placement: list it in source ABOVE ``@limiter.limit`` (just under
``@router.<verb>``). Decorators apply bottom-up, so ``confirm_token`` runs AFTER
the limiter — it stamps the marker on the *already limiter-wrapped* object, which
is exactly what FastAPI stores as ``route.endpoint``. That makes the marker
readable independent of any inner wrapper's ``functools.wraps`` behaviour. It does
NOT wrap the function (no new call frame, signature preserved) — it only stamps
the marker and returns the same object.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

F = TypeVar("F", bound=Callable[..., object])

#: Attribute name the route-contract derivation reads via ``getattr`` (single source).
CONFIRM_TOKEN_ATTR = "_confirm_token"  # noqa: S105 - attribute NAME, not a secret value


def confirm_token(token: str) -> Callable[[F], F]:
    """Mark a state-mutating handler as requiring ``?confirm=<token>``.

    Records *token* on the endpoint (``CONFIRM_TOKEN_ATTR``) so the route contract
    is derived from ``app.routes`` rather than a hand list. Does not alter behaviour
    — the handler body still performs the actual ``confirm != token`` check; this is
    the *declarative shadow* of that check, kept single-source by living on the same
    function.
    """
    if not isinstance(token, str) or not token:
        raise ValueError("confirm_token requires a non-empty token string")

    def deco(fn: F) -> F:
        setattr(fn, CONFIRM_TOKEN_ATTR, token)
        return fn

    return deco
