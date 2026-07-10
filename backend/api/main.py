"""backend/api/main.py

FastAPI application entry point.

All routes are registered here. The app is designed to be run with:
  uvicorn backend.api.main:app --host 0.0.0.0 --port 8080

On startup it initialises the state database and serves the frontend
static files from the compiled dist/ directory.
"""

from __future__ import annotations

from typing import Any
from collections.abc import AsyncIterator

from contextlib import asynccontextmanager
from pathlib import Path

import os

from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from backend import __version__
from backend.api import catalog as catalog_router
from backend.api import control_plane as control_plane_router
from backend.api import health as health_router
from backend.api import settings as settings_router
from backend.api import registry as registry_router
from backend.api import routing as routing_router
from backend.api import storage as storage_router
from backend.api import models as models_router
from backend.api import platform as platform_router
from backend.api import quickstart as quickstart_router
from backend.api.auth_policy import control_plane_guard  # #976 control-plane guard
from backend.api.middleware import (
    AuditLogMiddleware,
    CorrelationIdMiddleware,
    DeprecationHeaderMiddleware,
    PrivateIPTrustedHostMiddleware,
)
from backend.api.rate_limit import limiter
from backend.core.config import config
from backend.core.logging import configure_logging, get_logger
from backend.core.state import init_db

# Step 2.3 — configure structlog before any backend code logs at import time
# so the first line emitted shares the project schema (timestamp / level /
# logger / event / correlation_id / subsystem). Honours MS_LOG_LEVEL
# and MS_LOG_FORMAT env vars; falls back to DEBUG-when-debug / console.
configure_logging(level="DEBUG" if config.debug else "INFO")

log = get_logger(__name__)


def _recover_orphaned_installs() -> None:
    """On startup, mark any in-flight installs that never wrote __done__ as failed.

    If the server died mid-install, operation_steps has rows for that key but
    no __done__ sentinel. The progress poller would spin forever. Write a failed
    __done__ so the frontend can show an error instead of a hanging spinner.
    """
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            # Find all keys that have steps but no __done__
            rows = db._c.execute(
                """SELECT DISTINCT op_key FROM operation_steps
                   WHERE op_key NOT IN (
                       SELECT op_key FROM operation_steps WHERE step_name = '__done__'
                   )"""
            ).fetchall()
            orphaned = [r["op_key"] for r in rows]
            for key in orphaned:
                db.write_op_step(
                    key,
                    "__done__",
                    "error",
                    "Install did not complete — the server restarted mid-install. "
                    "Re-install the app to try again.",
                )
            if orphaned:
                log.warning(
                    "Recovered %d orphaned installs: %s",
                    len(orphaned),
                    ", ".join(orphaned),
                )
    except Exception as e:
        log.warning("Could not recover orphaned installs: %s", e)


async def _reconcile_on_startup() -> None:
    """Compare running containers against DB and log orphans. Non-blocking."""
    import asyncio as _asyncio

    await _asyncio.sleep(8)  # let service fully start first

    try:
        from backend.core.state import StateDB
        from backend.core.config import config as _cfg

        try:
            from backend.core import docker_client as _dc

            containers = _dc.list_containers() if hasattr(_dc, "list_containers") else []
            running_names = {c.name for c in containers}
        except Exception:
            return  # Docker not available

        _INFRA = {"traefik", "cloudflared", "tinyauth", "gluetun", "portainer"}

        with StateDB() as db:
            db_apps = {a.key: a for a in db.get_all_apps(status="running")}

        for name in running_names - _INFRA:
            if name not in db_apps:
                log.warning(
                    "Ghost container: '%s' is running but not in SLOP DB. "
                    "Use Settings → System Health to adopt or remove it.",
                    name,
                )

        for key, app in db_apps.items():
            cname = getattr(app, "container_name", key)
            if cname and cname not in running_names:
                log.warning(
                    "App '%s' is marked running but container '%s' not found — "
                    "may have stopped unexpectedly.",
                    key,
                    cname,
                )

        if _cfg.compose_dir.exists():
            for frag in _cfg.compose_dir.glob("*.yaml"):
                k = frag.stem
                if k in _INFRA or k in db_apps:
                    continue
                log.warning(
                    "Ghost compose fragment: '%s.yaml' has no DB entry — run ms-check for details.",
                    k,
                )

    except Exception as e:
        import logging as _l

        _l.getLogger(__name__).debug("Startup reconciliation skipped: %s", e)


def _cleanup_orphaned_records() -> None:
    """Remove DB records with no compose fragment and stale health data.

    Runs synchronously at startup before the server accepts requests.
    Safe to re-run — idempotent. Keeps the DB in sync with the filesystem.
    """
    try:
        from backend.core.state import StateDB
        from backend.core.config import config as _cfg
        import pathlib as _pl

        compose_dir: _pl.Path = _cfg.compose_dir
        if not compose_dir.exists():
            return

        INFRA = {
            "traefik",
            "tinyauth",
            "authelia",
            "cloudflared",
            "tailscale",
            "headscale",
            "gluetun",
            "glance",
            "homepage",
            "dockge",
            "dockhand",
            "komodo",
            "portainer",
            "portainer_be",
        }

        with StateDB() as db:
            # 1. Orphaned app records — DB entry but no compose fragment
            removed = []
            for app in db.get_all_apps():
                if app.key in INFRA:
                    continue
                if app.status in ("disabled", "removing"):
                    continue
                if not (compose_dir / f"{app.key}.yaml").exists():
                    db.execute("DELETE FROM apps WHERE key=?", (app.key,))
                    db.execute("DELETE FROM health_checks WHERE subject_key=?", (app.key,))
                    db.execute("DELETE FROM health_check_history WHERE subject_key=?", (app.key,))
                    db.execute("DELETE FROM operations WHERE subject_key=?", (app.key,))
                    try:
                        db.execute("DELETE FROM pending_fixes WHERE app_key=?", (app.key,))
                    except Exception:  # noqa: S110  # pending_fixes table may not exist in older DBs
                        pass
                    removed.append(app.key)
            if removed:
                # NOTE: StateDB auto-commits on __exit__ — db._conn.commit() removed (Core Rule 4.4)
                log.info(
                    "Startup cleanup: removed %d orphaned DB records: %s",
                    len(removed),
                    ", ".join(removed),
                )

            # 2. Stale health records for keys no longer in apps table
            stale = db.execute(
                "SELECT DISTINCT subject_key FROM health_checks "
                "WHERE subject_type='app' AND subject_key NOT IN (SELECT key FROM apps)"
            ).fetchall()
            if stale:
                db.execute(
                    "DELETE FROM health_checks WHERE subject_type='app' "
                    "AND subject_key NOT IN (SELECT key FROM apps)"
                )
                db.execute(
                    "DELETE FROM health_check_history WHERE subject_type='app' "
                    "AND subject_key NOT IN (SELECT key FROM apps)"
                )
                # NOTE: StateDB auto-commits on __exit__ — db._conn.commit() removed (Core Rule 4.4)
                log.info(
                    "Startup cleanup: cleared stale health records for: %s",
                    ", ".join(r[0] for r in stale),
                )

    except Exception as e:
        log.debug("Startup orphan cleanup skipped: %s", e)


def _emit_security_posture_warnings(app: FastAPI) -> None:
    """Emit WARNING-level log lines for potentially-risky configuration choices.

    Three conditions are checked:
      1. CORS allows all origins (``allow_origins`` contains ``"*"``).
      2. Swagger UI is enabled (``docs_url`` is not ``None``).
      3. No authentication middleware configured (``MS_AUTH_ENABLED`` not set
         to ``"true"``).

    All warnings are suppressed when the environment variable
    ``MS_SUPPRESS_SECURITY_WARNINGS=true`` is set.
    """
    if os.environ.get("MS_SUPPRESS_SECURITY_WARNINGS", "").lower() == "true":
        return

    if "*" in _cors_origins:
        log.warning("CORS allows all origins — restrict MS_CORS_ORIGINS for non-homelab use")

    if app.docs_url is not None:
        log.warning("Swagger UI is enabled — set MS_DISABLE_DOCS=true to disable for production")

    if os.environ.get("MS_AUTH_ENABLED", "").lower() != "true":
        log.warning("No auth middleware configured — SLOP is unauthenticated")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Startup and shutdown logic."""
    # Ensure data directory and database exist
    config.data_dir.mkdir(parents=True, exist_ok=True)
    config.compose_dir.mkdir(parents=True, exist_ok=True)
    init_db(config.db_path)
    log.info("SLOP backend ready — db: %s", config.db_path)

    # Emit security posture warnings for open/default configurations.
    _emit_security_posture_warnings(app)

    # Emit security posture warnings for open/default configs.
    _emit_security_posture_warnings(app)

    # Ensure tier-0 SLOP Agent record and baseline health check exist in DB.
    from backend.core.agent import ensure_agent_registered

    ensure_agent_registered()

    # #976 Phase-B: generate-if-absent the control-plane auth token into .env so enforce
    # mode (#1251) always has a real token to check. Single cross-path seam (deploy.sh seeds
    # it directly; the Python installer + existing-install upgrades land here). Idempotent —
    # writes only on first boot when no token is configured; never clobbers an operator value.
    from backend.api.settings import ensure_control_plane_token_provisioned

    try:
        ensure_control_plane_token_provisioned()
    except Exception as exc:  # provisioning must never block startup
        log.warning("control-plane token provisioning skipped: %s", exc)

    # Mark any in-flight installs as failed.
    # If the server restarted mid-install the __done__ sentinel was never written,
    # leaving the progress poller stuck. Clean those up on startup.
    _recover_orphaned_installs()
    # Clean up orphaned DB records and stale health data
    _cleanup_orphaned_records()
    # Ghost resource detection (runs in background, non-blocking)
    from backend.core.supervisor import spawn_supervised

    # Run-once supervised: a crash here is logged + recorded as an agent health
    # row instead of dying silently. No restart — reconcile is a one-shot.
    spawn_supervised("startup-reconcile", lambda: _reconcile_on_startup())

    # Start background health check scheduler.
    # It waits internally for the platform to be ready before running checks.
    from backend.health.scheduler import start_scheduler, stop_scheduler

    start_scheduler()

    # Phase F: Docker event watcher — detects container die/oom/unhealthy at runtime.
    # start_docker_event_watcher() spawns the watcher via spawn_supervised and
    # returns promptly, so it is awaited directly (no unsupervised create_task).
    from backend.agent.watcher import start_docker_event_watcher, stop_docker_event_watcher

    await start_docker_event_watcher()

    yield

    # Graceful shutdown — cancel the scheduler task and docker event watcher
    stop_scheduler()
    await stop_docker_event_watcher()
    log.info("SLOP backend stopping")


import os as _os  # noqa: E402  # deferred: app config depends on lifespan context manager being defined first

_disable_docs = _os.environ.get("MS_DISABLE_DOCS", "").lower() == "true"

app = FastAPI(
    title="SLOP",
    description="Self-hosted media stack manager",
    version=__version__,
    lifespan=lifespan,
    docs_url=None if _disable_docs else "/api/docs",
    redoc_url=None,
)

# CORS: allow all origins in debug mode; in production restrict to configured origins.
# The SPA on the same origin doesn't need CORS, but the CLI and external tools do.
# Set MS_CORS_ORIGINS="https://myapp.com,http://localhost:3000" in .env to restrict.
_cors_origins_env = _os.environ.get("MS_CORS_ORIGINS", "")
_cors_origins = (
    ["*"]
    if config.debug
    else [o.strip() for o in _cors_origins_env.split(",") if o.strip()]
    or ["*"]  # homelab default: allow all — restrict with MS_CORS_ORIGINS
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# TrustedHostMiddleware: closes audit-log-evasion gap from CVE-2026-48710 (Starlette
# BadHost) independent of Starlette version. See memory project-cve-2026-48710.
import os as _os_th  # noqa: E402  # deferred: app object must be created before middleware config

_ms_trusted_hosts_env = _os_th.environ.get("MS_TRUSTED_HOSTS", "")
if _ms_trusted_hosts_env.strip():
    # Explicit override — honour it exactly (stock exact/suffix matching).
    _trusted_hosts = [h.strip() for h in _ms_trusted_hosts_env.split(",") if h.strip()]
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=_trusted_hosts)
elif config.debug:
    # Dev convenience: accept any host in debug mode.
    log.info("TrustedHostMiddleware: debug=True — accepting all hosts ('*')")
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["*"])
else:
    # Production default: localhost variants + *.local (LAN) + config.domain if
    # set, PLUS any RFC1918 private IP (F10 — reach SLOP by raw LAN IP out of
    # the box; stock TrustedHostMiddleware can't express a CIDR range).
    _trusted_hosts = ["localhost", "127.0.0.1", "::1", "*.local"]
    _domain_env = _os_th.environ.get("DOMAIN", "").strip()
    if _domain_env:
        for _d in _domain_env.split(","):
            _d = _d.strip()
            if _d and _d not in _trusted_hosts:
                _trusted_hosts.append(_d)
    app.add_middleware(PrivateIPTrustedHostMiddleware, allowed_hosts=_trusted_hosts)

# Step 2.3.d — correlation-ID middleware. Sets a contextvar at request
# entry that every nested log line inherits, including async tasks.
# Echoes the ID back as `X-Request-ID` so callers can correlate logs.
app.add_middleware(CorrelationIdMiddleware)
# Step 3.2.d — deprecation-header middleware. Adds `Deprecation: true`
# + `Link: <successor>; rel=successor-version` + `Sunset: ...` to
# unversioned /api/<area>/... responses (ADR 0005).
app.add_middleware(DeprecationHeaderMiddleware)
# Step 4.3.c — audit log middleware. Records every POST/PUT/DELETE/
# PATCH request in audit_log table after the handler returns.
# See migrations/004_audit_log.sql for the schema rationale.
app.add_middleware(AuditLogMiddleware)

# Step 4.1 — Prometheus instrumentation. A thin, Starlette-correct
# ASGI middleware (backend/api/http_metrics.py) records request count +
# duration histograms per route template and exposes /metrics in the
# default Prometheus exposition format. Custom SLOP-specific metrics
# (install duration, health-check duration, DB query time, error
# counters) live in backend/core/metrics.py and join the same registry.
# Set MS_DISABLE_METRICS=true to skip mounting the /metrics endpoint.
_disable_metrics = _os.environ.get("MS_DISABLE_METRICS", "").lower() == "true"

if not _disable_metrics:
    from backend.api.http_metrics import install_http_metrics

    install_http_metrics(app)

# Step 4.2 — Kubernetes-style health probes. /healthz /readyz
# /startupz sit OUTSIDE the /api/v1/ versioning umbrella (they're
# operational infrastructure, not the application API).
from backend.api import probes as probes_router  # noqa: E402  # deferred: app object must exist before router registration

app.include_router(probes_router.router)

# Step 2.4 — rate limiter. Per-endpoint @limiter.limit(...) decorators
# tier the limits (heavy mutation: 5/min, heavy read: 10/min, light
# mutation: 30/min, default: 60/min). Localhost is bypassed.
from slowapi import _rate_limit_exceeded_handler  # noqa: E402  # deferred: app object must exist
from slowapi.errors import RateLimitExceeded  # noqa: E402

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]  # slowapi handler signature is narrower than Starlette's


# Step 4.1 wire-up: increment `slop_errors_total` on every
# unhandled exception that bubbles up to FastAPI. The handler then
# re-raises so Starlette's default 500 response logic still runs —
# this is metrics-only, not error-suppression.
@app.exception_handler(Exception)
async def _record_unhandled_error(
    request: Request,
    exc: Exception,
) -> Any:
    from backend.core.metrics import errors_total

    # Resolve route template (bounded cardinality) instead of the
    # literal URL — same discipline as audit middleware.
    template = request.url.path
    try:
        from starlette.routing import Match

        for route in request.app.router.routes:
            match, _scope = route.matches(request.scope)
            if match == Match.FULL:
                template = getattr(route, "path", template)
                break
    except Exception:  # noqa: S110  # best-effort route-template lookup; fall back to raw path
        pass
    try:
        errors_total.labels(
            endpoint=template,
            error_class=type(exc).__name__,
        ).inc()
    except Exception:  # noqa: S110  # metrics must never break error handling
        pass
    # Re-raise so Starlette's default 500 handler renders the response.
    raise exc


@app.exception_handler(OverflowError)
async def _overflow_to_400(request: Request, exc: OverflowError) -> JSONResponse:
    """An ``OverflowError`` reaching a request handler is always caused by an oversize
    client-supplied number — e.g. an int path/query param past SQLite's signed-64-bit
    range bound into a query (``OverflowError: Python int too large to convert to SQLite
    INTEGER``). Map the whole class to a 400 so no endpoint 5xx's on it (#1197), rather
    than guarding every individual int param. More specific than the ``Exception`` handler
    above, so Starlette dispatches here first (MRO-based dispatch).

    CAVEAT (review #1197): this is a CATCH-ALL — it cannot tell a client-input overflow
    from a genuine server-side one. There are no server-side ``OverflowError`` sources
    today (no ``struct.pack``/bit-shift/computed-arithmetic that can overflow), but if you
    ADD one, catch + handle it LOCALLY so a real server bug is not masqueraded as a 400."""
    return JSONResponse(status_code=400, content={"detail": "Numeric value out of range."})


# ── API routes (ALL must be registered BEFORE the SPA catch-all) ─────────


# Step 3.2: dual-mount every router at both `/api/v1/<area>` (the new
# canonical form) and `/api/<area>` (the legacy alias, deprecated as
# of 3.2). See `docs/adr/0005-api-versioning.md` for the policy.
def _mount(router_module: Any, name: str, tag: str) -> None:
    """Register `router_module.router` at both /api/v1/<name> and /api/<name>.
    The legacy /api/<name> mount carries a `deprecated` tag so Swagger
    UI groups it separately; the deprecation middleware below adds the
    `Deprecation: true` response header to unversioned requests.

    #976: the control_plane_guard is attached HERE — _mount is the sole
    include_router call site for area routers, so wiring the guard once covers
    every mounted area (incl. backend/agent/api.py) at BOTH dual-mounts. Default
    mode `off` makes the guard a no-op (zero behaviour change)."""
    app.include_router(
        router_module.router,
        prefix=f"/api/v1/{name}",
        tags=[tag],
        dependencies=[Depends(control_plane_guard)],
    )
    app.include_router(
        router_module.router,
        prefix=f"/api/{name}",
        tags=[tag, "deprecated"],
        dependencies=[Depends(control_plane_guard)],
    )


_mount(platform_router, "platform", "Platform")
_mount(registry_router, "registry", "Registry")
_mount(catalog_router, "catalog", "Catalog")
_mount(models_router, "models", "Models")
_mount(health_router, "health", "Health")
_mount(settings_router, "settings", "Settings")
_mount(routing_router, "routing", "Routing")
_mount(storage_router, "storage/sources", "Storage")
_mount(control_plane_router, "control-plane", "ControlPlane")  # #976 Phase-C posture badge

# Step 4 followup: quickstart.py's APIRouter carries its own
# `prefix="/quickstart"`, so the parent prefix is just `/api/v1` (or
# `/api`) without a name component. _mount can't express empty
# names (FastAPI rejects trailing-slash prefixes), so we handle the
# dual-mount directly here.
app.include_router(
    quickstart_router.router,
    prefix="/api/v1",
    tags=["QuickStart"],
    dependencies=[Depends(control_plane_guard)],  # #976
)
app.include_router(
    quickstart_router.router,
    prefix="/api",
    tags=["QuickStart", "deprecated"],
    dependencies=[Depends(control_plane_guard)],  # #976
)

# Late imports — these modules import the executor which in turn imports docker,
# so they are deferred to avoid import errors when docker is not installed.
from backend.api import apps as apps_router  # noqa: E402
from backend.api import infra as infra_router  # noqa: E402

_mount(apps_router, "apps", "Apps")
_mount(infra_router, "infra", "Infrastructure")

# Step 4.3.e — audit-log query endpoint. Read-only; the writing
# surface lives in AuditLogMiddleware above.
from backend.api import audit as audit_router  # noqa: E402

_mount(audit_router, "audit", "Audit")

# #733 — operations log query endpoint. Read-only; the writing
# surface lives in the executor and agent pipeline.
from backend.api import operations as operations_router  # noqa: E402

_mount(operations_router, "operations", "Operations")

# #778 — unified event timeline aggregator. Read-only; merges audit_log,
# operations, health_check_history, and cloud_llm_usage.
from backend.api import timeline as timeline_router  # noqa: E402

_mount(timeline_router, "timeline", "Timeline")

# Phase D — LLM agent diagnoses REST endpoints.
from backend.agent import api as agent_api_router  # noqa: E402

_mount(agent_api_router, "agent", "Agent")

# Container update preferences — proxies WUD update status and stores
# per-container notify/pin preferences in StateDB.
from backend.api import updates as updates_router  # noqa: E402

_mount(updates_router, "updates", "Updates")


# N6 — operator conversational control surface. text → intent → registry action
# (dispatched ONLY via registry.invoke_action / the shared governance gate).
from backend.api import chat as chat_router  # noqa: E402

_mount(chat_router, "chat", "Chat")


# ── System health endpoint ────────────────────────────────────────────────


@app.get("/api/ping", tags=["System"])
@app.get("/api/health", tags=["System"])  # backward-compat alias
def ping() -> dict[str, Any]:
    return {"status": "ok", "version": __version__}


@app.get(
    "/api/coverage", dependencies=[Depends(control_plane_guard)]
)  # #976: @app-level exec surface
def get_coverage_map() -> dict[str, Any]:
    """Return the latest coverage map generated by ms-coverage.

    Used by the topology dashboard to show live coverage state.
    Regenerates the map on each call if data is stale (>1 hour).

    Defined here (above the SPA catch-all) — was previously below
    `/{full_path:path}` and silently shadowed for GET requests.
    """
    import json as _j
    import time as _t
    import subprocess as _sp
    from backend.core.config import config as _cfg

    out = (
        (_cfg.repo_root if hasattr(_cfg, "repo_root") else _cfg.data_dir.parent)
        / "data"
        / "coverage_map.json"
    )
    try:
        # Regenerate if stale or missing
        if not out.exists() or (_t.time() - out.stat().st_mtime > 3600):
            _sp.run(
                [
                    "python3",
                    str(
                        (_cfg.repo_root if hasattr(_cfg, "repo_root") else _cfg.data_dir.parent)
                        / "ms-coverage"
                    ),
                    "--json",
                ],
                capture_output=True,
                timeout=30,
                cwd=str(_cfg.repo_root if hasattr(_cfg, "repo_root") else _cfg.data_dir.parent),
            )
        if out.exists():
            data: dict[str, Any] = _j.loads(out.read_text())
            return data
    except Exception:  # noqa: S110  # best-effort coverage map; return degraded response if unavailable
        pass
    return {"error": "Coverage map not available. Run ms-coverage to generate."}


# ── Serve Vue frontend (MUST be last — catch-all shadows earlier routes) ──
# CRITICAL: Any include_router() call after this point will be unreachable
# for GET requests because /{full_path:path} matches everything.

_static = config.static_dir
if _static.exists():
    _assets = _static / "assets"
    if _assets.exists():
        app.mount("/assets", StaticFiles(directory=_assets), name="assets")

    # Serve root-level static files (favicon, icons) explicitly
    # — must come before the SPA fallback or they'd get index.html
    _root_files = {
        "favicon.svg": "image/svg+xml",
        "favicon.ico": "image/x-icon",
        "apple-touch-icon.png": "image/png",
        "icon-192.png": "image/png",
        "icon-512.png": "image/png",
    }
    for _fname, _mime in _root_files.items():
        _fpath = _static / _fname
        if _fpath.exists():

            def _make_route(path: Path = _fpath, mime: str = _mime) -> Any:
                @app.get(f"/{path.name}", include_in_schema=False)
                def _static_file() -> FileResponse:
                    return FileResponse(path, media_type=mime)

            _make_route()

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str) -> FileResponse:
        """Return index.html for all non-API GET requests (Vue Router client-side routing).

        Sets Cache-Control: no-cache so browsers always revalidate index.html
        after a deploy — the JS chunks inside it have content hashes, so a stale
        index.html references deleted chunks and breaks lazy-loaded views.
        """
        index = _static / "index.html"
        if not index.exists():
            from fastapi import HTTPException

            raise HTTPException(
                status_code=503, detail="Frontend not built. Run: cd frontend && npm run build"
            )
        return FileResponse(index, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
else:
    log.warning(
        "Frontend static dir not found: %s — UI will not be available. "
        "Build with: cd frontend && npm run build",
        _static,
    )


# (`get_coverage_map` was defined above — moved before the SPA catch-all
#  since `/{full_path:path}` shadowed it for GET requests.)
