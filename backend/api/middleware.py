"""backend/api/middleware.py — FastAPI middleware.

`CorrelationIdMiddleware` (step 2.3.d) reads the incoming `X-Request-ID`
header, generating a fresh UUID if absent, and stores it in the
`correlation_id` contextvar so every log line in the request handler
chain shares it. The same value is echoed back as a response header
so callers can correlate client and server logs.

`DeprecationHeaderMiddleware` (step 3.2.d) flags requests to the
unversioned `/api/<area>/<path>` form with a `Deprecation: true`
response header pointing at the `/api/v1/...` successor. See
`docs/adr/0005-api-versioning.md` for the policy.

`AuditLogMiddleware` (step 4.3.c) records POST/PUT/DELETE/PATCH
requests to the `audit_log` table — actor, action (route template),
resource id, request-body hash (sha256, NOT the body), response
status, correlation id. Schema rationale lives in
`migrations/004_audit_log.sql`.

See `docs/cleanup/STEP_2_3_STRUCTURED_LOGGING_STRATEGY.md` §4 for
correlation-ID context.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
import time
import uuid
from typing import Any
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import PlainTextResponse
from starlette.routing import Match

from backend.core.logging import (
    get_logger,
    set_correlation_id,
    reset_correlation_id,
)

_audit_log = get_logger(__name__)


_HEADER = "X-Request-ID"


class PrivateIPTrustedHostMiddleware(BaseHTTPMiddleware):
    """Host-header allow-list like Starlette's TrustedHostMiddleware, but it
    ALSO accepts any RFC1918 private IP (10/8, 172.16/12, 192.168/16) and
    loopback as the Host (F10).

    Why a custom middleware: Starlette's `TrustedHostMiddleware` matches exact
    hostnames or a single suffix wildcard (`*.foo`) only — it cannot express a
    CIDR range. So a self-hoster reaching SLOP by raw LAN IP (e.g.
    `http://192.168.1.50:8080`) gets a 400 "invalid host header" out of the box.
    Accepting private IPs by default stays safe against the threat this gate
    exists for (CVE-2026-48710 audit-log evasion / DNS rebinding): that attack
    needs a *public* Host name (`Host: evil.com`) resolving to a private IP, not
    a literal private-IP Host. A public-facing deploy sets `DOMAIN` behind
    Traefik and keeps strict hostname matching; `MS_TRUSTED_HOSTS` remains an
    exact override (main.py uses stock `TrustedHostMiddleware` for that branch).

    Repro: `Host: 192.168.1.50:8080` → 400 before this fix, 200 after.
    """

    def __init__(self, app: Any, allowed_hosts: list[str]) -> None:
        super().__init__(app)
        self._allowed = list(allowed_hosts)
        self._allow_any = "*" in self._allowed

    def _host_ok(self, raw_host: str) -> bool:
        if self._allow_any:
            return True
        hostname = raw_host.split(":")[0]
        if not hostname:
            return False
        # Exact / suffix-wildcard match (TrustedHostMiddleware semantics).
        for pattern in self._allowed:
            if hostname == pattern:
                return True
            if pattern.startswith("*") and hostname.endswith(pattern[1:]):
                return True
        # CIDR can't be expressed above — accept any private/loopback IP literal.
        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            return False
        return ip.is_private or ip.is_loopback

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if self._host_ok(request.headers.get("host", "")):
            return await call_next(request)
        return PlainTextResponse("Invalid host header", status_code=400)


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    """Inject `X-Request-ID` into the contextvar; echo on response."""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        cid = (
            request.headers.get(_HEADER)
            or request.headers.get(_HEADER.lower())
            or str(uuid.uuid4())
        )
        token = set_correlation_id(cid)
        try:
            response = await call_next(request)
        finally:
            reset_correlation_id(token)
        response.headers[_HEADER] = cid
        return response


# Routes that aren't part of the application API and so are NOT versioned.
# These get NO deprecation header even though they sit under /api/.
_API_NONVERSIONED_PATHS: frozenset[str] = frozenset(
    {
        "/api/ping",
        "/api/health",
        "/api/coverage",
    }
)


# Path prefix `/api/v<N>/` is the canonical versioned form for ANY
# integer N. Anticipates the v2 / v3 / ... cutovers per the playbook
# at `docs/cleanup/STEP_3_2_V2_PLAYBOOK.md` — when a new version
# lands, its routes are recognised as canonical out of the box (no
# middleware change needed). The legacy `/api/<area>` form gets the
# deprecation tripod; the versioned form does not.
_API_VERSIONED_PREFIX = re.compile(r"^/api/v\d+/")

# Soft-deprecation sunset target. RFC 8594 sunset header value.
# See ADR 0005 § "Deprecation policy" for the rationale.
_API_SUNSET_HTTP_DATE = "Mon, 01 Sep 2026 00:00:00 GMT"


class DeprecationHeaderMiddleware(BaseHTTPMiddleware):
    """Add `Deprecation: true` + `Link: ...; rel=successor-version` +
    `Sunset: ...` headers to responses for unversioned `/api/<area>`
    requests. Versioned `/api/v1/...` requests, infrastructure routes
    (`/api/ping`, `/api/health`, `/api/coverage`), and non-API routes
    (`/`, `/assets/...`, the SPA fallback) pass through unchanged.

    The middleware is response-side only — request handling is
    unaffected. Per ADR 0005 the dual-mount means the legacy path and
    the v1 path resolve to the same handler with identical behaviour;
    only the response headers differ.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        response = await call_next(request)
        path = request.url.path
        # Only flag /api/<area>/... that is NOT /api/v1/... and is not
        # one of the small set of non-versioned infrastructure routes.
        if (
            path.startswith("/api/")
            and not _API_VERSIONED_PREFIX.match(path)
            and path not in _API_NONVERSIONED_PATHS
        ):
            response.headers["Deprecation"] = "true"
            successor = "/api/v1/" + path[len("/api/") :]
            response.headers["Link"] = f'<{successor}>; rel="successor-version"'
            response.headers["Sunset"] = _API_SUNSET_HTTP_DATE
        return response


# Methods that mutate state and so MUST be audited. GET, HEAD, OPTIONS
# are read-only by HTTP semantics — auditing them would balloon the
# table without value.
_AUDIT_METHODS: frozenset[str] = frozenset({"POST", "PUT", "DELETE", "PATCH"})

# Routes excluded from audit even if they use a mutating method. The
# /metrics endpoint registers internally as POST in some clients;
# skip the operational-infrastructure routes since they're not part
# of the application API.
_AUDIT_PATH_BLOCKLIST: frozenset[str] = frozenset(
    {
        "/metrics",
        "/healthz",
        "/readyz",
        "/startupz",
    }
)


def _route_template(request: Request) -> str:
    """Resolve the request to its FastAPI route template (e.g.
    `/api/v1/apps/{key}/install`) so audit log entries have bounded
    cardinality. Falls back to the literal URL path when the request
    didn't match any registered route — that case usually 404s
    anyway, but recording the attempted path is informative."""
    app = request.app
    for route in app.router.routes:
        match, _scope = route.matches(request.scope)
        if match == Match.FULL:
            return getattr(route, "path", request.url.path)
    return request.url.path


def _resource_id(request: Request) -> str | None:
    """Extract the path parameter values from a matched route — these
    identify the mutated resource. Multiple params concatenated with
    '/'. Returns None when the route has no path params."""
    params = request.scope.get("path_params") or {}
    if not params:
        return None
    return "/".join(str(v) for v in params.values())


class AuditLogMiddleware(BaseHTTPMiddleware):
    """Append one row to `audit_log` per mutating request.

    The middleware runs AFTER the handler so it can record the
    response status. On any exception during the audit write, the
    middleware logs and continues — auditing must never break a
    request. The state DB may also be unconfigured (early-startup,
    test fixtures); handled the same way.
    """

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        # Read body BEFORE handler (Starlette consumes it once);
        # re-attach via the Request._body trick for downstream code.
        body_bytes = b""
        if request.method in _AUDIT_METHODS:
            try:
                body_bytes = await request.body()
                request._body = body_bytes
            except Exception:
                body_bytes = b""

        response = await call_next(request)

        # Only audit mutating methods + non-blocked paths
        if request.method not in _AUDIT_METHODS or request.url.path in _AUDIT_PATH_BLOCKLIST:
            return response

        try:
            from backend.core.logging import get_correlation_id
            from backend.core.state import StateDB

            body_hash = hashlib.sha256(body_bytes).hexdigest() if body_bytes else None
            template = _route_template(request)
            resource = _resource_id(request)
            corr = get_correlation_id()
            with StateDB() as db:
                db.execute(
                    "INSERT INTO audit_log "
                    "(ts, actor, action, resource_id, request_body_hash, "
                    " response_status, correlation_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        int(time.time()),
                        "local",
                        f"{request.method} {template}",
                        resource,
                        body_hash,
                        response.status_code,
                        corr,
                    ),
                )
        except Exception as e:
            # Never let audit failure break the request. Surface in
            # the log so operators can see audit going dark.
            _audit_log.warning("audit_log write failed: %s", e)

        return response
