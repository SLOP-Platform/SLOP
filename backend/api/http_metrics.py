"""backend/api/http_metrics.py — HTTP request metrics (step 4.1).

A thin, starlette-correct replacement for the
``prometheus-fastapi-instrumentator`` library. That library's
``routing.py`` does a bare ``route.path`` on every matched route, which
raises ``AttributeError`` under Starlette 1.x because routes registered
via ``include_router`` are reached through an ``_IncludedRouter`` object
that has no ``.path`` attribute (it stores per-route templates on its
``effective_candidates()`` instead). The result: a fresh/CI install
(Starlette 1.x) would 500 on every request. Local dev masked it because
it pinned Starlette 0.52.1.

This module records the same two HTTP series the library produced, on
the *default* ``prometheus_client`` registry so the custom ``slop_*``
metrics defined in ``backend/core/metrics.py`` join the same exposition:

  - ``http_requests_total``            {method, handler, status}  (Counter)
  - ``http_request_duration_seconds``  {method, handler, status}  (Histogram)

Behaviour mirrors the previous Instrumentator configuration:
  - status codes are *not* grouped (literal "200"/"404"/... strings),
  - requests with no matched templated route are skipped
    (``should_ignore_untemplated=True``) for cardinality control,
  - the ``/metrics`` and ``/openapi.json`` handlers are excluded.

``handler`` is always the bounded route *template*
(``/api/v1/apps/{key}``), never the literal URL — same cardinality
discipline as ``backend/core/metrics.py`` and the audit middleware.

Implemented as a pure-ASGI middleware (not ``BaseHTTPMiddleware``) to
avoid that base class's response-streaming and background-task pitfalls.
``install_http_metrics(app)`` wires the middleware and registers the
``GET /metrics`` route.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Histogram,
    generate_latest,
)
from starlette.requests import Request
from starlette.responses import Response
from starlette.routing import Match, Mount

from backend.core.logging import get_logger

if TYPE_CHECKING:
    from fastapi import FastAPI
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = get_logger(__name__)


# ── HTTP request series (default registry — joins slop_* customs) ──────────

# Default histogram buckets (seconds) — the prometheus_client defaults,
# which match what the old Instrumentator emitted for request latency.
_DURATION_BUCKETS = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.25,
    0.5,
    0.75,
    1.0,
    2.5,
    5.0,
    7.5,
    10.0,
)

http_requests_total = Counter(
    "http_requests_total",
    "Total HTTP requests by method, handler (route template) and status",
    labelnames=("method", "handler", "status"),
)

http_request_duration_seconds = Histogram(
    "http_request_duration_seconds",
    "HTTP request duration in seconds by method, handler and status",
    labelnames=("method", "handler", "status"),
    buckets=_DURATION_BUCKETS,
)


# Handlers excluded from instrumentation entirely (matches the old
# ``excluded_handlers``). Compared against the resolved route template.
_EXCLUDED_HANDLERS = frozenset({"/metrics", "/openapi.json"})


def resolve_route_template(routes: object, scope: Scope) -> str | None:
    """Resolve the matched route's bounded *template* for ``scope``.

    Returns the full, prefix-included template (``/api/v1/apps/{key}``),
    or ``None`` if no templated route matches (e.g. a 404, or an
    un-routed path) — the caller treats ``None`` as "untemplated" and
    skips it.

    Starlette-version-correct. Under Starlette 1.x, routers included via
    ``include_router`` appear in the route tree as ``_IncludedRouter``,
    whose per-route templates (with the include prefix already applied)
    live on its ``effective_candidates()`` — each candidate exposes its
    own ``.matches()`` plus a full ``.path_format``/``.path``. Under
    Starlette 0.52 the routes are flat with the full template on the
    ``Route`` directly. Both paths are handled.

    Every attribute access is guarded so this function NEVER raises —
    metric labelling must not be able to break request handling. (This
    is precisely the failure mode that broke the upstream library.)
    """
    try:
        route_list = list(routes)  # type: ignore[call-overload]
    except Exception:
        return None

    for route in route_list:
        template = _template_for_matched_route(route, scope)
        if template is not None:
            return template
    return None


def _template_from_included_router(route: Any, scope: Scope) -> str | None:
    """Template from a Starlette 1.x ``_IncludedRouter``'s effective candidates.

    Each candidate carries the full (prefixed) template and its own
    ``matches()``. Guarded so it never raises; a candidate whose match probe
    errors is skipped (cmatch left None), not propagated."""
    try:
        candidates = list(route.effective_candidates())
    except Exception:
        return None
    for cand in candidates:
        cmatch = None
        try:
            cmatch, _cchild = cand.matches(scope)
        except Exception:
            cmatch = None
        if cmatch == Match.FULL:
            template = getattr(cand, "path_format", None) or getattr(cand, "path", None)
            if template:
                return str(template)
    return None


def _template_for_matched_route(route: Any, scope: Scope) -> str | None:
    """Template for a single route iff it FULL-matches ``scope``, else None.

    Split out of resolve_route_template so both stay simple and use return-None
    (not except/continue). Every attribute access is guarded — labelling must
    never raise into request handling (the upstream-library failure mode)."""
    match = None
    try:
        match, _child = route.matches(scope)
    except Exception:
        return None
    if match != Match.FULL:
        return None

    # Starlette 1.x included router (carries the full prefixed template).
    if type(route).__name__ == "_IncludedRouter":
        return _template_from_included_router(route, scope)

    # Mounted sub-app/router: recurse with the mount prefix applied.
    if isinstance(route, Mount):
        prefix = getattr(route, "path", "") or ""
        sub_scope = scope
        try:
            _m, child = route.matches(scope)
            sub_scope = dict(scope)
            sub_scope.update(child)
        except Exception:
            sub_scope = scope
        inner = resolve_route_template(getattr(route, "routes", ()), sub_scope)
        return (prefix + inner) if inner else None

    # Plain route (Starlette 0.52 flat tree, or a top-level Route on 1.x).
    template = getattr(route, "path_format", None) or getattr(route, "path", None)
    return str(template) if template else None


class HttpMetricsMiddleware:
    """Pure-ASGI middleware recording per-request count + duration.

    Runs *outside* the router, so by the time the inner app returns the
    route tree can be re-matched to recover the bounded template. The
    response status is captured by wrapping ``send`` and watching for the
    ``http.response.start`` message — no response buffering, no
    background-task interference.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        status_holder: dict[str, int] = {}

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                status_holder["status"] = int(message["status"])
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            self._record(scope, status_holder, time.perf_counter() - start)

    def _record(self, scope: Scope, status_holder: dict[str, int], elapsed: float) -> None:
        try:
            app = scope.get("app")
            routes = getattr(getattr(app, "router", None), "routes", ())
            handler = resolve_route_template(routes, scope)
            # should_ignore_untemplated: skip anything with no matched
            # templated route (404s, /docs static, etc.).
            if handler is None:
                return
            if handler in _EXCLUDED_HANDLERS:
                return
            method = scope.get("method", "")
            # should_group_status_codes=False: literal status code string.
            status = str(status_holder.get("status", 0))
            http_requests_total.labels(method=method, handler=handler, status=status).inc()
            http_request_duration_seconds.labels(
                method=method, handler=handler, status=status
            ).observe(elapsed)
        except Exception:
            logger.debug("http metric record failed", exc_info=True)


async def _metrics_endpoint(_request: Request) -> Response:
    """Render the default registry in Prometheus exposition format."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )


def install_http_metrics(app: FastAPI) -> None:
    """Wire the HTTP metrics middleware and the ``GET /metrics`` route.

    Importing ``backend.core.metrics`` registers the custom ``slop_*``
    metrics on the same default registry so they surface at ``/metrics``.

    The route is registered as a *plain Starlette* route via
    ``app.router.add_route`` rather than ``add_api_route``: this is a raw
    exposition endpoint that takes no parameters, and FastAPI's
    parameter-introspection on ``add_api_route`` would otherwise treat
    the handler argument as a request field and 422 the request. A
    Starlette route is also placed ahead of the SPA catch-all in the
    route list, so the catch-all does not shadow ``/metrics``.
    ``include_in_schema=False`` keeps it out of the OpenAPI spec,
    matching the old ``expose()`` behaviour.
    """
    import importlib

    importlib.import_module(
        "backend.core.metrics"
    )  # register slop_* metrics on the default registry

    app.add_middleware(HttpMetricsMiddleware)
    app.router.add_route(
        "/metrics",
        _metrics_endpoint,
        methods=["GET"],
        include_in_schema=False,
    )
