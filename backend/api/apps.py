"""backend/api/apps.py

App lifecycle API routes.

GET  /api/apps                    — list all installed apps
GET  /api/apps/{key}              — single app detail + health
POST /api/apps/{key}/install      — install from catalog
DELETE /api/apps/{key}            — remove app
POST /api/apps/{key}/replace/{new_key} — replace with different app
POST /api/apps/{key}/restart      — restart container
GET  /api/apps/{key}/logs         — recent container logs
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

import threading as _threading

from backend.api.rate_limit import limiter
from backend.core import docker_client
import time as _time

from backend.api.apps_validate import sanitize_manifest
from backend.core.error_detail import safe_detail
from backend.core.logging import get_logger
from backend.core.path_guard import PathNotAllowed, safe_component
from backend.core.state import StateDB

# Install priority for batch preflight topological sort.
# TODO: move to manifests
_INSTALL_PRIORITY: dict[str, int] = {
    "prowlarr": 0,
    "sabnzbd": 1,
    "qbittorrent": 1,
    "plex": 2,
    "jellyfin": 2,
    "emby": 2,
    "sonarr": 3,
    "radarr": 3,
    "lidarr": 3,
    "readarr": 3,
    "bazarr": 4,
    "seerr": 5,
}

# Module-level cache for update-check results keyed by app key.
# Avoids blocking a docker manifest inspect (30s timeout) per request.
# Structure: {key: {"result": dict[str, Any], "ts": float}}
_update_check_cache: dict[str, dict[str, Any]] = {}


def _get_cached_update_result(key: str) -> dict[str, Any] | None:
    """Extract the update-check result dict from the cache, or None if absent."""
    entry = _update_check_cache.get(key)
    if entry is None:
        return None
    result: dict[str, Any] = entry["result"]
    return result


# Track in-progress installs to prevent duplicate concurrent installs
_installing: set[str] = set()
# Serializes check-and-add on _installing so concurrent API calls for the
# same key see a consistent view (closes id=460: lock was defined but never
# acquired around _installing mutations).
_install_lock = _threading.Lock()
# Cap concurrent batch installs at 6 to avoid overwhelming the Docker daemon.
_INSTALL_SEMAPHORE = _threading.Semaphore(6)

from backend.manifests.executor import (  # noqa: E402  # deferred to avoid circular import at module init
    ExecutionResult,
    install_app,
    remove_app,
    replace_app,
    run_wiring_pass,
)

log = get_logger(__name__)
router = APIRouter()


def _safe_key(key: str) -> str:
    """Validate a route ``{key}`` path-param as a safe single path segment BEFORE it is
    used to build a filesystem path (#1103, CodeQL py/path-injection). The shared guard for
    the apps.py write sinks that join ``{key}`` onto a real directory (update_app frag,
    enhance_custom_app manifest, get_app_config dir). Returns the key unchanged when safe;
    raises HTTP 400 on traversal/charset violations. Mirrors the registry/health seam (#1041)."""
    try:
        return safe_component(key, field="key")
    except PathNotAllowed as e:
        raise HTTPException(
            status_code=400, detail=safe_detail(e, "Invalid app key.", log=log)
        ) from e


# ── Port detection sentinel ───────────────────────────────────────────────
# Returned by _get_listening_ports() when all detection methods fail (e.g.
# inside a restricted container with no /proc access and no ss/netstat).
# Callers MUST check `result is PORTS_INDETERMINATE` before treating the
# value as a set — an empty set means "nothing listening", this sentinel
# means "we don't know".
class _PortsIndeterminate:
    """Singleton sentinel: port state is unknown, all detection paths failed."""

    _instance: _PortsIndeterminate | None = None

    def __new__(cls) -> _PortsIndeterminate:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "PORTS_INDETERMINATE"

    def __bool__(self) -> bool:
        return False


PORTS_INDETERMINATE = _PortsIndeterminate()


# Community manifest sanitization (id=472) extracted to apps_validate.py
# (#1302 drain) — sanitize_manifest re-imported at top.


# ── SLOP-managed env vars (always written to .env by the wizard) ──────────
# These are excluded from missing_vars in the linter and variable scanner
# because SLOP owns their values — they should never be left for the user
# to fill in manually (the platform sets them from the wizard's DB record).
_SLOP_MANAGED_VARS: frozenset[str] = frozenset(
    {
        "PUID",
        "PGID",
        "TZ",
        "DOMAIN",
        "CONFIG_ROOT",
        "MEDIA_ROOT",
        "CF_TUNNEL_TOKEN",
        "CF_DNS_API_TOKEN",
        "TAILSCALE_AUTH_KEY",
        "TINYAUTH_USERNAME",
        "TINYAUTH_PASSWORD",
        "TINYAUTH_AUTH_USERS",
        "VPN_TYPE",
        "VPN_SERVICE_PROVIDER",
        "WIREGUARD_PRIVATE_KEY",
        "OPENVPN_USER",
        "OPENVPN_PASSWORD",
        # Generated by the setup wizard on every SLOP install — suppress linter false positives
        "POSTGRES_PASSWORD",
        "POSTGRES_USER",
        # Generated by the managed mariadb service on first provision (#1203);
        # MARIADB_USER/MARIADB_DATABASE use ${..:-booklore} defaults so they self-resolve.
        "MARIADB_ROOT_PASSWORD",
        "MARIADB_PASSWORD",
    }
)


# ── Request / Response models ─────────────────────────────────────────────


class InstallRequest(BaseModel):
    extra_env: dict[str, str] | None = None
    host_port: int | None = None
    user_volume_paths: dict[str, str] | None = None  # install_prompts key→value (id=816)


class RemoveRequest(BaseModel):
    delete_config: bool | None = None  # None = retain, True = delete, False = retain


class StepLogOut(BaseModel):
    name: str
    status: str
    message: str
    detail: str = ""


class ExecutionOut(BaseModel):
    ok: bool
    app_key: str
    operation: str
    steps: list[StepLogOut]
    error: str = ""


class AppOut(BaseModel):
    key: str
    display_name: str
    category: str
    tier: int
    status: str
    image: str
    image_tag: str
    web_port: int | None
    host_port: int | None
    config_path: str | None
    installed_at: int
    last_healthy_at: int | None
    criticality: str
    container_status: str | None = None
    container_health: str | None = None


# ── Helpers ───────────────────────────────────────────────────────────────


def _exec_to_out(r: ExecutionResult) -> ExecutionOut:
    return ExecutionOut(
        ok=r.ok,
        app_key=r.app_key,
        operation=r.operation,
        steps=[StepLogOut(**s.__dict__) for s in r.steps],
        error=r.error,
    )


def _app_with_container(app: Any) -> AppOut:
    """Enrich app state record with live container status.

    Handles Docker being unavailable gracefully — returns DB status
    with container fields empty rather than crashing.
    """
    try:
        c = docker_client.get_container(app.key)
    except (docker_client.DockerError, Exception):
        c = None  # Docker unavailable — use DB status only
    return AppOut(
        key=app.key,
        display_name=app.display_name,
        category=app.category,
        tier=app.tier,
        status=app.status,
        image=app.image,
        image_tag=app.image_tag,
        web_port=app.web_port,
        host_port=app.host_port,
        config_path=app.config_path,
        installed_at=app.installed_at,
        last_healthy_at=app.last_healthy_at,
        criticality=get_criticality(app.key).value,
        container_status=c.status if c else None,
        container_health=c.health if c else None,
    )


# ── Routes ────────────────────────────────────────────────────────────────


@router.get("", response_model=list[AppOut])
def list_apps() -> list[AppOut]:
    """List all apps known to SLOP (installed or previously installed).

    Uses a single Docker API call (containers.list) to fetch live status for
    all apps instead of one call per app, avoiding the N+1 Docker API pattern
    that caused ~100ms-per-app latency on the dashboard load.
    """
    with StateDB() as db:
        apps = db.get_all_apps()
    # Batch-fetch container info once rather than N separate get_container() calls
    containers = docker_client.get_containers_by_name([a.key for a in apps])
    return [
        AppOut(
            key=a.key,
            display_name=a.display_name,
            category=a.category,
            tier=a.tier,
            status=a.status,
            image=a.image,
            image_tag=a.image_tag,
            web_port=a.web_port,
            host_port=a.host_port,
            config_path=a.config_path,
            installed_at=a.installed_at,
            last_healthy_at=a.last_healthy_at,
            criticality=get_criticality(a.key).value,
            container_status=containers[a.key].status if a.key in containers else None,
            container_health=containers[a.key].health if a.key in containers else None,
        )
        for a in apps
    ]


# NOTE: This static route MUST be declared before /{key} so FastAPI matches
# "installs" literally rather than treating it as a {key} path parameter.
@router.get("/installs/progress")
def all_installs_progress() -> dict[str, Any]:
    """Status of all apps with an active or recent install operation.

    Returns apps that have any step written in the last 600 seconds OR
    any app with an unacknowledged __done__ step.

    Shape: {"apps": {key: {"done": bool, "ok": bool|None, "steps": list, "error": str|None}}}
    """
    import time as _time

    now = int(_time.time())
    cutoff = now - 600

    from backend.core.state import StateDB as _StateDB

    with _StateDB() as db:
        # Find all op_keys that have recent steps OR unacknowledged __done__
        rows = db._c.execute(
            """SELECT DISTINCT op_key FROM operation_steps
               WHERE created_at >= ? OR step_name = '__done__'""",
            (cutoff,),
        ).fetchall()
        op_keys = [r["op_key"] for r in rows]

        result: dict[str, Any] = {}
        for key in op_keys:
            steps = db.get_op_steps(key)
            done_step = next((s for s in steps if s["step"] == "__done__"), None)
            visible = [s for s in steps if not s["step"].startswith("__")]
            result[key] = {
                "done": done_step is not None,
                "ok": done_step["status"] == "ok" if done_step else None,
                "steps": visible,
                "error": done_step["message"]
                if done_step and done_step["status"] == "error"
                else None,
            }

    return {"apps": result}


@router.get("/{key}", response_model=AppOut)
def get_app(key: str) -> AppOut:
    """Get a single app's state and live container status.

    Uses get_containers_by_name([key]) — consistent with list_apps() batch call —
    rather than the per-container get_container() to avoid a separate Docker API call.
    """
    with StateDB() as db:
        app = db.get_app(key)
    if app is None:
        raise HTTPException(status_code=404, detail=f"App '{key}' is not installed.")
    containers = docker_client.get_containers_by_name([key])
    c = containers.get(key)
    return AppOut(
        key=app.key,
        display_name=app.display_name,
        category=app.category,
        tier=app.tier,
        status=app.status,
        image=app.image,
        image_tag=app.image_tag,
        web_port=app.web_port,
        host_port=app.host_port,
        config_path=app.config_path,
        installed_at=app.installed_at,
        last_healthy_at=app.last_healthy_at,
        criticality=get_criticality(key).value,
        container_status=c.status if c else None,
        container_health=c.health if c else None,
    )


@router.post("/{key}/install")
@limiter.limit("5/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped (Step 2.4 — heavy mutation tier)
async def api_install(
    request: Request,
    key: str,
    background_tasks: BackgroundTasks,
    req: InstallRequest = Body(default_factory=InstallRequest),
) -> dict[str, Any]:
    """Install an app from the catalog.

    Returns immediately with {installing: true, key}.
    Poll GET /{key}/install/progress for real-time step updates.
    The install runs in a background thread — POST returns in <1ms.
    """
    # Validate app exists in catalog before starting background task
    from backend.manifests.loader import load_all_manifests

    _manifests = load_all_manifests()
    if key not in _manifests:
        raise HTTPException(
            status_code=404,
            detail=f"App '{key}' not found in catalog. Check the key and try again.",
        )

    # Prevent duplicate concurrent installs of the same key.
    # _install_lock serializes the check-and-add so two simultaneous POST
    # requests for the same key cannot both see an empty _installing set
    # and both proceed (closes id=460).
    with _install_lock:
        if key in _installing:
            raise HTTPException(
                status_code=409,
                detail=f"'{key}' is already being installed. Poll /{key}/install/progress for status.",
            )
        _installing.add(key)

    # Clear any previous step records for this key
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            db.clear_op_steps(key)
            db.write_op_step(key, "queued", "running", f"Install queued for {key}…")
    except Exception:  # noqa: S110  # best-effort progress tracking; install proceeds even if DB unavailable
        pass

    def _run() -> None:
        result = install_app(
            key,
            extra_env=req.extra_env,
            host_port_override=req.host_port,
            user_volume_paths=req.user_volume_paths or {},
        )
        # Write sentinel so the poller knows the install is complete.
        try:
            from backend.core.state import StateDB

            with StateDB() as db:
                if result.ok:
                    db.write_op_step(key, "__done__", "ok", "Installation complete.")
                else:
                    db.write_op_step(
                        key,
                        "__done__",
                        "error",
                        result.error or "Installation failed.",
                    )
        except Exception:  # noqa: S110  # best-effort progress sentinel; install outcome already determined
            pass
        finally:
            _installing.discard(key)  # release lock regardless of outcome

    background_tasks.add_task(_run)
    return {
        "installing": True,
        "key": key,
        "message": f"Installing {key}… poll /{key}/install/progress",
    }


@router.get("/{key}/install/progress")
def api_install_progress(key: str) -> dict[str, Any]:
    """Poll for real-time install progress.

    Returns steps written so far. When a step named '__done__' appears,
    the install is complete. Check its status for ok/error.

    Frontend algorithm:
      1. POST /{key}/install → start install
      2. Poll GET /{key}/install/progress every 500ms
      3. When steps contains __done__, stop polling and show result
    """
    from backend.core.state import StateDB

    with StateDB() as db:
        steps = db.get_op_steps(key)

    done_step = next((s for s in steps if s["step"] == "__done__"), None)
    visible = [s for s in steps if not s["step"].startswith("__")]

    return {
        "key": key,
        "done": done_step is not None,
        "ok": done_step["status"] == "ok" if done_step else None,
        "steps": visible,
        "error": done_step["message"] if done_step and done_step["status"] == "error" else None,
    }


@router.get("/{key}/install/stream")
async def api_install_stream(key: str) -> StreamingResponse:
    """SSE stream of install progress for a single app.

    Connects to the op_steps table and streams new rows as they arrive.
    Sends 'data: <json>\\n\\n' events. Closes when '__done__' step appears.

    Frontend connects with EventSource('/api/v1/apps/{key}/install/stream').
    Falls back gracefully if SSE is not supported — existing poll endpoint unchanged.
    """

    async def _generate() -> AsyncIterator[str]:
        import time as _time

        seen: set[Any] = set()
        deadline = _time.monotonic() + 600
        while _time.monotonic() < deadline:
            with StateDB() as db:
                rows = db.get_op_steps(key)
            for row in rows:
                row_id = row.get("id") or row.get("step")
                if row_id in seen:
                    continue
                seen.add(row_id)
                yield f"data: {json.dumps(row)}\n\n"
                if row.get("step") == "__done__":
                    return
            await asyncio.sleep(0.3)
        yield 'data: {"step":"__done__","status":"error","message":"stream timeout"}\n\n'

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/batch/prefetch")
@limiter.limit("3/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped; batch prefetch is heavier than single-app
async def batch_prefetch(
    request: Request,
    req: BatchInstallRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, Any]:
    """Start pulling images for a list of apps in the background.

    Called when the user reaches the Review stage — gives pull a head start
    before Deploy is clicked. Non-blocking: returns immediately.

    Pulls images directly from manifests (compose fragments don't exist
    pre-install, so compose pull would silently no-op). All pulls run
    concurrently via asyncio.gather.
    """
    from backend.manifests.loader import load_all_manifests

    all_manifests = load_all_manifests()
    valid = [k for k in req.keys if k in all_manifests]

    async def _prefetch_async() -> None:
        async def _pull_one(image_ref: str) -> None:
            try:
                proc = await asyncio.create_subprocess_exec(
                    "docker",
                    "pull",
                    image_ref,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                await proc.communicate()
            except Exception as e:
                log.debug("prefetch pull failed for %s: %s", image_ref, e)

        pulls = []
        for key in valid:
            m = all_manifests.get(key)
            if m is None:
                continue
            image = getattr(m, "image", None)
            if not image:
                continue  # no image field — skip
            image_tag = getattr(m, "image_tag", "latest") or "latest"
            image_ref = f"{image}:{image_tag}"
            pulls.append(_pull_one(image_ref))

        if pulls:
            await asyncio.gather(*pulls)

    background_tasks.add_task(_prefetch_async)
    return {
        "ok": True,
        "prefetching": valid,
        "message": f"Pre-pulling {len(valid)} image(s) in background.",
    }


@router.delete("/{key}")
@limiter.limit("5/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped (Step 2.4 — heavy mutation tier)
def api_remove(
    request: Request,
    key: str,
    background_tasks: BackgroundTasks,
    req: RemoveRequest = Body(default_factory=RemoveRequest),
) -> dict[str, Any]:
    """Remove an installed app.

    Returns immediately with {removing: true, key}.
    Poll GET /{key}/install/progress for step updates.
    docker-compose down can take 10-30s -- runs in a background thread.

    Pass delete_config=true to also delete the app's config folder.
    If delete_config is omitted, the config folder is retained (safe default).
    """
    # Verify the app exists before starting the background task
    with StateDB() as db:
        app = db.get_app(key)
    if app is None:
        raise HTTPException(status_code=404, detail=f"App '{key}' is not installed.")

    # Clear previous progress and mark as queued
    try:
        with StateDB() as db:
            db.clear_op_steps(key)
            db.write_op_step(key, "queued", "running", f"Remove queued for {key}…")
    except Exception:  # noqa: S110  # best-effort progress tracking; remove proceeds even if DB unavailable
        pass

    _delete_config = req.delete_config

    def _run() -> None:
        result = remove_app(key, delete_config=_delete_config)
        try:
            with StateDB() as db:
                if result.ok:
                    db.write_op_step(key, "__done__", "ok", "Removal complete.")
                else:
                    db.write_op_step(
                        key,
                        "__done__",
                        "error",
                        result.error or "Removal failed.",
                    )
        except Exception:  # noqa: S110  # best-effort progress sentinel
            pass

    background_tasks.add_task(_run)
    return {"removing": True, "key": key, "message": "Remove started in background."}


@router.post("/{key}/replace/{new_key}")
@limiter.limit("5/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped (Step 2.4 — heavy mutation tier)
def api_replace(
    request: Request,
    key: str,
    new_key: str,
    background_tasks: BackgroundTasks,
    req: InstallRequest = Body(default_factory=InstallRequest),
) -> dict[str, Any]:
    """Replace an installed app with a different one.

    Returns immediately with {replacing: true, key: new_key, old_key: key}.
    Poll GET /{new_key}/install/progress for step updates.
    replace_app() installs, rewires, and removes — can take minutes — runs
    in a background thread.

    The old app's config folder is always retained — remove manually if needed.
    """
    # Verify the old app exists before starting the background task
    with StateDB() as db:
        app = db.get_app(key)
    if app is None:
        raise HTTPException(status_code=404, detail=f"App '{key}' is not installed.")

    # Clear previous progress for new_key and mark as queued
    try:
        with StateDB() as db:
            db.clear_op_steps(new_key)
            db.write_op_step(new_key, "queued", "running", f"Replace {key} → {new_key} queued…")
    except Exception:  # noqa: S110  # best-effort progress tracking; replace proceeds even if DB unavailable
        pass

    _extra_env = req.extra_env

    def _run() -> None:
        result = replace_app(key, new_key, extra_env=_extra_env)
        try:
            with StateDB() as db:
                if result.ok:
                    db.write_op_step(new_key, "__done__", "ok", "Replace complete.")
                else:
                    db.write_op_step(
                        new_key,
                        "__done__",
                        "error",
                        result.error or "Replace failed.",
                    )
        except Exception:  # noqa: S110  # best-effort progress sentinel
            pass

    background_tasks.add_task(_run)
    return {
        "replacing": True,
        "key": new_key,
        "old_key": key,
        "message": "Replace started in background.",
    }


@router.post("/{key}/restart", response_model=dict)
def api_restart(key: str) -> dict[str, Any]:
    """Restart an app's container."""
    with StateDB() as db:
        app = db.get_app(key)
    if app is None:
        raise HTTPException(status_code=404, detail=f"App '{key}' is not installed.")
    try:
        c = docker_client.client().containers.get(key)
        c.restart(timeout=30)
        return {"ok": True, "message": f"Container '{key}' restarted."}
    except Exception as e:
        raise HTTPException(
            status_code=503,
            detail=safe_detail(e, f"Could not restart '{key}'. Is Docker running?", log=log),
        ) from e


@router.post("/{key}/rewire", response_model=dict)
def api_rewire(key: str) -> dict[str, Any]:
    """Manually trigger a wiring pass for a specific app.

    Useful when an app's wiring rows are missing or stuck in 'failed' state.
    Safe to call multiple times — INSERT OR IGNORE prevents duplicate rows.
    """
    with StateDB() as db:
        app = db.get_app(key)
    if app is None:
        raise HTTPException(status_code=404, detail=f"App '{key}' is not installed.")
    from backend.manifests.executor import run_wiring_pass

    result = run_wiring_pass({key})
    return {"ok": True, "result": result}


@router.get("/{key}/logs")
def api_logs(key: str, tail: int = Query(default=100, le=500)) -> dict[str, Any]:
    """Get recent container logs."""
    with StateDB() as db:
        app = db.get_app(key)
    if app is None:
        raise HTTPException(status_code=404, detail=f"App '{key}' is not installed.")
    try:
        logs = docker_client.container_logs(key, tail=tail)
        return {"key": key, "logs": logs}
    except docker_client.DockerError as e:
        raise HTTPException(
            status_code=503,
            detail=safe_detail(
                e, f"Docker unavailable — cannot retrieve logs for '{key}'.", log=log
            ),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=503, detail=safe_detail(e, "Could not retrieve logs.", log=log)
        ) from e


# ---------------------------------------------------------------------------
# Disable / Enable / Criticality routes (Step 4 addition)
# ---------------------------------------------------------------------------

from backend.manifests.executor import (  # noqa: E402  # deferred: additional symbols used only by routes below
    Criticality,
    DisableResult,
    disable_app,
    enable_app,
    get_criticality,
    PERF_THRESHOLDS,
)
from backend.core.system_eval import evaluate_system, SystemProfile  # noqa: E402
from pydantic import BaseModel as _BaseModel  # noqa: E402


class DisableRequest(_BaseModel):
    reason: str = "user_request"


class DisableOut(_BaseModel):
    ok: bool
    key: str
    criticality: str
    warning: str | None = None
    error: str | None = None


class SystemProfileOut(_BaseModel):
    cpu_cores: int
    cpu_model: str
    total_ram_gb: float
    free_ram_gb: float
    headroom_ram_gb: float
    docker_ram_gb: float
    architecture: str
    disks: list[dict[str, Any]]
    estimated_stack_ram_gb: float
    recommended_llm_model: str
    available_llm_models: list[str]
    llm_warning: str | None
    measured_at: int
    note: str


def _criticality_warning(key: str, crit: Criticality) -> str | None:
    if crit == Criticality.IMPORTANT:
        return (
            f"'{key}' is marked IMPORTANT. Disabling interrupts remote access "
            f"and authentication. LAN access remains unaffected."
        )
    return None


@router.post("/{key}/disable", response_model=DisableOut)
def api_disable(key: str, req: DisableRequest = Body(default_factory=DisableRequest)) -> DisableOut:
    """Gracefully disable an app.

    Stops the container and renames its compose fragment to .yaml.disabled.
    Config, state, and wiring are preserved. Re-enable with /enable.
    Inviolable apps (Traefik) cannot be disabled.

    Performance-triggered disables from the health system also route here
    with reason='performance' or 'health'.
    """
    crit = get_criticality(key)
    warning = _criticality_warning(key, crit)

    result: DisableResult = disable_app(key, reason=req.reason)

    if not result.ok:
        err = result.error or "disable failed"
        if "not installed" in err.lower() or "not found" in err.lower():
            status = 404
        elif "inviolable" in err.lower():
            status = 409
        else:
            status = 500
        raise HTTPException(status_code=status, detail=err)

    return DisableOut(
        ok=True,
        key=key,
        criticality=result.criticality,
        warning=warning,
    )


@router.post("/{key}/enable", response_model=DisableOut)
def api_enable(key: str) -> DisableOut:
    """Re-enable a previously disabled app.

    Restores the compose fragment and starts the container.
    Wiring is marked pending for the next health cycle to reconnect.
    """
    result: DisableResult = enable_app(key)

    if not result.ok:
        # 422 for expected failures (missing fragment = reinstall needed)
        # 404 if app not found
        code = 404 if "not installed" in (result.error or "") else 422
        raise HTTPException(status_code=code, detail=result.error)

    return DisableOut(ok=True, key=key, criticality=result.criticality)


@router.get("/{key}/criticality")
def api_criticality(key: str) -> dict[str, Any]:
    """Return the criticality classification for an app.

    Criticality determines what happens when the app is disabled:
      INVIOLABLE  — cannot disable, stack depends on it
      IMPORTANT   — warn before disabling (auth/tunnel providers)
      INDEPENDENT — disable freely, no stack impact
      ENHANCEMENT — disabling has zero availability impact
    """
    with StateDB() as db:
        app = db.get_app(key)
    if not app:
        raise HTTPException(status_code=404, detail=f"App '{key}' is not installed.")
    crit = get_criticality(key)
    return {
        "key": key,
        "criticality": crit.value,
        "can_disable": crit != Criticality.INVIOLABLE,
        "warning": _criticality_warning(key, crit),
        "perf_thresholds": PERF_THRESHOLDS,
    }


# ---------------------------------------------------------------------------
# System profile / resource evaluation
# ---------------------------------------------------------------------------


@router.get("/system/profile", response_model=SystemProfileOut, tags=["System"])
def api_system_profile() -> SystemProfileOut:
    """Run a system resource evaluation.

    Returns hardware specs, current RAM usage, estimated stack RAM
    for all installed apps, headroom, and LLM model recommendation.

    All figures are estimates — clearly labelled as such in the response.
    """
    with StateDB() as db:
        p = db.get_platform()
        installed_keys = [a.key for a in db.get_all_apps()]

    try:
        profile: SystemProfile = evaluate_system(
            selected_app_keys=installed_keys,
            config_root=p.config_root or "/",
            media_root=p.media_root or "/",
        )
    except Exception as _e:
        raise HTTPException(status_code=503, detail=f"System evaluation failed: {_e}") from _e

    return SystemProfileOut(
        cpu_cores=profile.cpu_cores,
        cpu_model=profile.cpu_model,
        total_ram_gb=round(profile.total_ram_mb / 1024, 1),
        free_ram_gb=round(profile.free_ram_mb / 1024, 1),
        headroom_ram_gb=round(profile.headroom_ram_mb / 1024, 1),
        docker_ram_gb=round(profile.docker_container_ram_mb / 1024, 1),
        architecture=profile.architecture,
        disks=[
            {
                "path": d.path,
                "total_gb": d.total_gb,
                "free_gb": d.free_gb,
                "percent_used": d.percent_used,
            }
            for d in profile.disks
        ],
        estimated_stack_ram_gb=round(profile.estimated_stack_ram_mb / 1024, 1),
        recommended_llm_model=profile.recommended_model,
        available_llm_models=profile.available_models,
        llm_warning=profile.llm_warning,
        measured_at=profile.measured_at,
        note=(
            "RAM figures are estimates. Actual usage varies with library size, "
            "active streams, and container configuration."
        ),
    )


# ── Batch install ─────────────────────────────────────────────────────────


class BatchInstallRequest(BaseModel):
    keys: list[str] = Field(default=[], alias="app_keys")
    preflight_only: bool = False

    model_config = {"populate_by_name": True}


class PreflightIssue(BaseModel):
    level: str  # error | warning | info
    message: str
    affected: list[str] = []


class PreflightResult(BaseModel):
    install_order: list[str]
    issues: list[PreflightIssue]
    can_proceed: bool


@router.post("/batch/preflight")
def batch_preflight(req: BatchInstallRequest) -> PreflightResult:
    """Analyse a list of apps before batch install.

    Returns:
    - install_order: topologically sorted install sequence
    - issues: errors (blocking) and warnings (informational)
    - can_proceed: False if any blocking errors exist
    """
    from backend.manifests.loader import load_all_manifests
    from backend.core.state import StateDB

    issues: list[PreflightIssue] = []
    all_manifests = load_all_manifests()

    with StateDB() as db:
        installed = {a.key for a in db.get_all_apps()}

    # 1. Validate all keys exist in catalog
    unknown = [k for k in req.keys if k not in all_manifests]
    if unknown:
        issues.append(
            PreflightIssue(
                level="error",
                message=f"Not in catalog: {', '.join(unknown)}",
                affected=unknown,
            )
        )

    valid_keys = [k for k in req.keys if k in all_manifests]

    # 2. Resolve dependencies — add missing requires
    to_install = set(valid_keys)
    missing_deps: list[str] = []
    for key in valid_keys:
        m = all_manifests[key]
        for req_key in m.requires:
            if req_key not in installed and req_key not in to_install:
                to_install.add(req_key)
                missing_deps.append(req_key)

    if missing_deps:
        issues.append(
            PreflightIssue(
                level="warning",
                message=f"Added required dependencies: {', '.join(missing_deps)}",
                affected=missing_deps,
            )
        )

    # 3. Check for already-installed apps
    already = [k for k in req.keys if k in installed]
    if already:
        issues.append(
            PreflightIssue(
                level="info",
                message=f"Already installed (will skip): {', '.join(already)}",
                affected=already,
            )
        )

    # 4. Topological sort — deps before dependents
    install_order = sorted(
        to_install - installed,
        key=lambda k: (_INSTALL_PRIORITY.get(k, 10), k),
    )

    return PreflightResult(
        install_order=install_order,
        issues=issues,
        can_proceed=not any(i.level == "error" for i in issues),
    )


@router.post("/batch/install")
@limiter.limit("3/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped; batch install is heavier than single-app
def batch_install(request: Request, req: BatchInstallRequest) -> dict[str, Any]:
    """Start a batch install. All apps run concurrently (capped at 6 by semaphore).
    Poll GET /apps/{key}/install/progress for per-app status.
    """
    import threading

    # Run preflight first
    preflight = batch_preflight(req)
    if not preflight.can_proceed:
        return {
            "ok": False,
            "error": "Pre-flight check failed — see issues",
            "preflight": preflight.dict(),
        }

    keys = preflight.install_order

    def _run_batch() -> None:
        from backend.manifests.loader import load_all_manifests

        # Build dep_map for post-install dep-skip attribution.
        try:
            all_manifests = load_all_manifests()
            dep_map: dict[str, set[str]] = {
                k: set(getattr(all_manifests[k], "requires", [])) & set(keys)
                for k in keys
                if k in all_manifests
            }
        except Exception:
            dep_map = {}

        failed_keys: set[str] = set()

        def _install_one(key: str) -> tuple[str, bool, str]:
            """Install a single app; returns (key, ok, error_msg)."""
            with _install_lock:
                if key in _installing:
                    return key, True, ""  # another call owns it; treat as non-failure
                _installing.add(key)
            try:
                with StateDB() as db:
                    db.clear_op_steps(key)
                    db.write_op_step(key, "queued", "running", "Queued for batch install…")
                result = install_app(key)
                with StateDB() as db:
                    if result.ok:
                        db.write_op_step(key, "__done__", "ok", "Installed.")
                    else:
                        db.write_op_step(key, "__done__", "error", result.error or "Failed.")
                return key, result.ok, result.error or ""
            except Exception as e:
                try:
                    with StateDB() as db:
                        db.write_op_step(key, "__done__", "error", str(e))
                except Exception:  # noqa: S110  # best-effort progress update; inner failure must not mask outer
                    pass
                return key, False, str(e)
            finally:
                _installing.discard(key)

        def _install_one_guarded(key: str) -> tuple[str, bool, str]:
            with _INSTALL_SEMAPHORE:
                return _install_one(key)

        # All apps run concurrently, gated by semaphore to cap Docker daemon load.
        with ThreadPoolExecutor(max_workers=max(1, min(len(keys), 6))) as pool:
            futures = {pool.submit(_install_one_guarded, k): k for k in keys}
            for fut in as_completed(futures):
                _key, ok, _err = fut.result()
                if not ok:
                    failed_keys.add(_key)

        # Post-install: clarify error messages for apps whose deps also failed.
        for key in list(failed_keys):
            deps_failed = sorted(dep_map.get(key, set()) & failed_keys)
            if deps_failed:
                try:
                    with StateDB() as db:
                        db.write_op_step(
                            key,
                            "__done__",
                            "error",
                            f"Skipped — required app(s) failed: {', '.join(deps_failed)}",
                        )
                except Exception:  # noqa: S110  # best-effort progress update
                    pass

        # Post-install wiring pass: write wiring rows for apps that installed successfully.
        try:
            run_wiring_pass({k for k in keys if k not in failed_keys})
        except Exception as _we:
            log.warning("Post-install wiring pass failed: %s", _we)

    threading.Thread(target=_run_batch, daemon=True).start()

    dep_skipped = [k for k in req.keys if k not in keys]

    # Pre-flight companion scan: find required wiring targets not yet installed.
    # Uses the parsed wiring field on AppManifest (loader.py WireDef).
    # Only flags connects_to wires that are required (optional=False) and whose
    # peer is absent from the already-installed set.
    from backend.manifests.loader import load_all_manifests as _lam
    from backend.core.state import StateDB as _SDB

    _all_manifests = _lam()
    with _SDB() as _db:
        _installed = {a.key for a in _db.get_all_apps()}
    missing_required: list[dict[str, str]] = []
    for _key in keys:
        _manifest = _all_manifests.get(_key)
        if _manifest is None:
            continue
        for _wire in getattr(_manifest, "wiring", []) or []:
            if (
                getattr(_wire, "direction", "") == "connects_to"
                and not getattr(_wire, "optional", True)
                and getattr(_wire, "peer", None)
                and _wire.peer not in _installed
                and _wire.peer not in set(keys)
            ):
                missing_required.append(
                    {
                        "source": _key,
                        "companion": _wire.peer,
                        "wire_type": getattr(_wire, "wire_type", ""),
                        "description": getattr(
                            _wire, "description", f"{_key} requires {_wire.peer}"
                        ),
                    }
                )

    # Collect install_prompts for all apps being installed (id=816).
    # The frontend wizard displays these as form fields before deploying.
    # TODO: frontend wizard fields for install_prompts
    install_prompts_by_app: dict[str, list[dict[str, Any]]] = {}
    for _key in keys:
        _manifest = _all_manifests.get(_key)
        if _manifest and getattr(_manifest, "install_prompts", None):
            install_prompts_by_app[_key] = list(_manifest.install_prompts)

    return {
        "ok": True,
        "install_order": keys,
        "dep_skipped": dep_skipped,
        "preflight": preflight.dict(),
        "required_companions": missing_required,
        "install_prompts": install_prompts_by_app,
    }


# ── YAML compose linter ────────────────────────────────────────────────────


class LintComposeRequest(BaseModel):
    """Typed request body for POST /lint-compose (id=466)."""

    yaml: str = Field("", description="docker-compose.yml fragment to validate")


class LintResult(BaseModel):
    valid: bool
    errors: list[str] = []
    warnings: list[str] = []
    manifest_preview: dict[str, Any] | None = None
    missing_vars: list[str] = []  # ${VAR} refs in compose YAML not found in platform .env
    port_conflicts: list[
        dict[str, Any]
    ] = []  # [{port, type, conflicting}] — structured conflict data


def _get_listening_ports() -> set[int] | _PortsIndeterminate:
    """Return the set of TCP ports in LISTEN state, or PORTS_INDETERMINATE.

    Detection order (each tried in sequence, first success wins):
      1. /proc/net/tcp and /proc/net/tcp6  — direct kernel file read
      2. ``ss -tln``                        — iproute2 socket statistics
      3. ``netstat -tln``                   — net-tools fallback

    Returns PORTS_INDETERMINATE (not an empty set) when every method fails,
    so callers can distinguish "nothing listening" from "we don't know".
    """
    # ── Method 1: /proc/net/tcp ───────────────────────────────────────────
    proc_ports: set[int] = set()
    proc_readable = False
    for fname in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(fname) as fh:
                lines = fh.read().splitlines()
            proc_readable = True
            for line in lines[1:]:  # skip header row
                parts = line.split()
                if len(parts) < 4:
                    continue
                if parts[3] != "0A":  # 0A = TCP_LISTEN
                    continue
                local_addr = parts[1]
                if ":" not in local_addr:
                    continue
                hex_port = local_addr.split(":")[-1]
                try:
                    proc_ports.add(int(hex_port, 16))
                except ValueError:
                    pass
        except OSError:
            pass
    if proc_readable:
        return proc_ports

    # ── Method 2: ss -tln ────────────────────────────────────────────────
    try:
        ss_result = subprocess.run(
            ["ss", "-tln"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        ss_ports: set[int] = set()
        for line in ss_result.stdout.splitlines()[1:]:  # skip header
            parts = line.split()
            # ss output: Netid State Recv-Q Send-Q Local:Port Peer:Port
            if len(parts) < 5:
                continue
            local = parts[4]
            if ":" not in local:
                continue
            try:
                ss_ports.add(int(local.rsplit(":", 1)[-1]))
            except ValueError:
                pass
        return ss_ports
    except (OSError, FileNotFoundError, subprocess.SubprocessError):
        pass

    # ── Method 3: netstat -tln ────────────────────────────────────────────
    try:
        ns_result = subprocess.run(
            ["netstat", "-tln"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        ns_ports: set[int] = set()
        for line in ns_result.stdout.splitlines():
            parts = line.split()
            # netstat: Proto Recv-Q Send-Q LocalAddr ForeignAddr State
            if len(parts) < 4 or parts[0] not in ("tcp", "tcp6"):
                continue
            if len(parts) >= 6 and parts[5] != "LISTEN":
                continue
            local = parts[3]
            if ":" not in local:
                continue
            try:
                ns_ports.add(int(local.rsplit(":", 1)[-1]))
            except ValueError:
                pass
        return ns_ports
    except (OSError, FileNotFoundError, subprocess.SubprocessError):
        pass

    # ── All methods failed ────────────────────────────────────────────────
    log.warning(
        "_get_listening_ports: /proc/net/tcp unreadable and ss/netstat unavailable; "
        "returning INDETERMINATE"
    )
    return PORTS_INDETERMINATE


def _read_env_keys() -> set[str]:
    """Return the set of variable names currently defined in the platform .env file."""
    from backend.core.config import config as _cfg

    env_path = _cfg.env_file
    keys: set[str] = set()
    if not env_path.exists():
        return keys
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, _ = line.partition("=")
            keys.add(k.strip())
    return keys


@router.post("/lint-compose")
def lint_compose_yaml(payload: LintComposeRequest) -> LintResult:
    """Parse and validate a docker-compose.yml fragment.

    Checks:
    - Valid YAML syntax
    - Has a services: block with exactly one service
    - Service has an image: field
    - Ports are in the correct format
    - Volumes are in the correct format
    - Generates a SLOP manifest preview if valid
    """
    import yaml as _yaml

    errors: list[str] = []
    warnings: list[str] = []

    raw = payload.yaml
    if not raw.strip():
        return LintResult(valid=False, errors=["Paste a docker-compose.yml fragment to validate."])

    # Parse YAML
    try:
        doc = _yaml.safe_load(raw)
    except _yaml.YAMLError as e:
        line = getattr(getattr(e, "problem_mark", None), "line", None)
        detail = f" (line {line + 1})" if line is not None else ""
        return LintResult(
            valid=False,
            errors=[f"YAML syntax error{detail}: {e.problem if hasattr(e, 'problem') else str(e)}"],
        )

    if not isinstance(doc, dict):
        return LintResult(
            valid=False, errors=["Expected a YAML mapping (key: value) at the top level."]
        )

    # Services block
    services = doc.get("services", {})
    if not services:
        # Maybe they pasted a bare service def without the `services:` wrapper
        # Try to treat the whole doc as a single service
        if "image" in doc:
            services = {"app": doc}
            warnings.append(
                "No 'services:' wrapper found — treating entire YAML as a single service definition."
            )
        else:
            errors.append(
                "No 'services:' block found. Expected: services:\\n  myapp:\\n    image: ..."
            )
            return LintResult(valid=False, errors=errors, warnings=warnings)

    if len(services) > 1:
        warnings.append(
            f"Found {len(services)} services — SLOP will use the first one. Consider a single-service fragment."
        )

    # Validate first service
    svc_name = next(iter(services))
    svc = services[svc_name] or {}

    if not isinstance(svc, dict):
        errors.append(f"Service '{svc_name}' definition is not a mapping.")
        return LintResult(valid=False, errors=errors)

    # Required: image
    image = svc.get("image", "")
    if not image:
        errors.append(f"Service '{svc_name}' is missing an 'image:' field.")
    elif ":" not in image:
        warnings.append(
            f"Image '{image}' has no tag — will use ':latest'. Pin a version for stability."
        )

    # Ports check
    ports = svc.get("ports", [])
    for p in ports:
        if isinstance(p, str):
            parts = p.split(":")
            if len(parts) == 2:
                try:
                    int(parts[1])
                except ValueError:
                    errors.append(f"Invalid port format: '{p}'. Expected 'host:container'.")
            elif len(parts) != 1:
                errors.append(f"Invalid port format: '{p}'.")

    # Port conflict check — extract host ports and compare against installed apps + system
    port_conflicts: list[dict[str, Any]] = []
    host_ports_to_check: list[int] = []
    for p in ports:
        pstr = str(p)
        if ":" in pstr:
            parts_pc = pstr.split(":")
            # Handle both host:container (2-part) and bind_ip:host:container (3-part)
            host_idx = 0 if len(parts_pc) == 2 else 1
            try:
                host_ports_to_check.append(int(parts_pc[host_idx]))
            except (ValueError, IndexError):
                pass

    if host_ports_to_check:
        from backend.core.state import StateDB as _StateDB

        with _StateDB() as _db_pc:
            installed_port_map: dict[int, str] = {
                a.host_port: a.display_name
                for a in _db_pc.get_all_apps()
                if a.host_port is not None
            }
        sys_ports = _get_listening_ports()
        # sys_ports may be PORTS_INDETERMINATE when all detection methods fail.
        # In that case we skip the system-port check (we cannot raise a false alarm).
        _sys_known = sys_ports is not PORTS_INDETERMINATE
        for hport in host_ports_to_check:
            if hport in installed_port_map:
                app_nm = installed_port_map[hport]
                warnings.append(
                    f"Port conflict: {hport} is already used by installed app '{app_nm}'. "
                    f"The container will fail to bind this port — choose a different host port."
                )
                port_conflicts.append(
                    {"port": hport, "type": "installed_app", "conflicting": app_nm}
                )
            elif _sys_known and hport in sys_ports:  # type: ignore[operator]
                warnings.append(
                    f"Port {hport} is already bound on this host. "
                    f"Another process is listening — the container may fail to start."
                )
                port_conflicts.append(
                    {"port": hport, "type": "system", "conflicting": "system process"}
                )

    # Volumes check
    volumes = svc.get("volumes", [])
    for v in volumes:
        if isinstance(v, str) and ":" not in v:
            warnings.append(
                f"Volume '{v}' has no container path — it will be a named volume with no bind mount."
            )

    # Environment variables — warn about hardcoded secrets
    env = svc.get("environment", {})
    env_list = env if isinstance(env, list) else [f"{k}={v}" for k, v in (env or {}).items()]
    for entry in env_list:
        estr = str(entry)
        if any(word in estr.upper() for word in ("PASSWORD", "SECRET", "TOKEN", "KEY", "API")):
            if "=" in estr and not estr.endswith("=") and "${" not in estr:
                warnings.append(
                    f"Env var appears to contain a hardcoded secret: '{estr.split('=')[0]}'. Use ${{VAR}} references instead."
                )

    if errors:
        return LintResult(valid=False, errors=errors, warnings=warnings)

    # Build manifest preview
    image_parts = image.split(":")
    web_port = None
    for p in ports:
        pstr = str(p)
        if ":" in pstr:
            try:
                web_port = int(pstr.split(":")[-1])
                break
            except ValueError:
                pass

    manifest_preview = {
        "key": svc_name.lower().replace("-", "_"),
        "display_name": svc_name.replace("-", " ").replace("_", " ").title(),
        "image": image_parts[0],
        "image_tag": image_parts[1] if len(image_parts) > 1 else "latest",
        "web_port": web_port,
        "volumes": {
            v.split(":")[1].split(":")[0].strip("/").replace("/", "_"): v.split(":")[1]
            if ":" in str(v)
            else v
            for v in volumes[:4]
            if v
        },
        "category": "tools",
        "tier": 2,
        "service_type": "management",
    }

    # ── Env extraction ────────────────────────────────────────────────────
    # Preserve the service's environment block in the manifest so installed
    # fragments carry the user's ${VAR} references through to the compose file.
    env_block = svc.get("environment") or {}
    env_dict: dict[str, str] = {}
    if isinstance(env_block, list):
        for item in env_block:
            item_str = str(item).strip()
            if "=" in item_str:
                k, _, v = item_str.partition("=")
                env_dict[k.strip()] = v.strip()
            elif item_str:
                env_dict[item_str] = ""  # bare var name — inherits from .env
    elif isinstance(env_block, dict):
        for k, v in env_block.items():
            env_dict[str(k).strip()] = str(v).strip() if v is not None else ""

    if env_dict:
        manifest_preview["env"] = env_dict

    # ── Variable scan ─────────────────────────────────────────────────────
    # Find ${VAR} references without :- defaults (truly required — empty at
    # runtime if not in .env).  Refs with defaults (${VAR:-x}) are optional
    # and won't be empty, so we don't prompt the user for those.
    import re as _re

    required_refs = set(_re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", raw))
    known_keys = _read_env_keys()
    missing_vars = sorted(
        v for v in required_refs if v not in known_keys and v not in _SLOP_MANAGED_VARS
    )

    if missing_vars:
        warnings.append(
            f"{len(missing_vars)} variable(s) not found in your .env: "
            + ", ".join(missing_vars)
            + " — you will be prompted to provide values before install."
        )

    return LintResult(
        valid=True,
        errors=[],
        warnings=warnings,
        manifest_preview=manifest_preview,
        missing_vars=missing_vars,
        port_conflicts=port_conflicts,
    )


# ── Shared: community manifest normalization and save ────────────────────────


def _save_community_manifest(
    manifest_data: dict[str, Any],
    compose_yaml: str = "",
    source_url: str = "",
) -> dict[str, Any]:
    """Normalize a manifest dict and save it to the community catalog.

    Single source of truth for all non-catalog install paths.
    Applies all compliance rules:
      - Key sanitization (filesystem-safe identifier)
      - Complete manifest fields (traefik, health, linuxserver, ports)
      - Category validation
      - Community catalog persistence

    Returns: {"ok": True, "key": str, "install_url": str}
    Raises: HTTPException on validation failure.

    Called by: install_from_github, install_custom_app
    """
    import re as _re
    import yaml as _yaml
    from backend.core.config import config as _cfg

    # ── Sanitize community manifest before any processing (id=472) ───────
    # Strip disallowed keys, shell metacharacters, and path traversal sequences
    # from all string values before validating or persisting the manifest.
    manifest_data = sanitize_manifest(manifest_data)

    # ── Validate required fields ──────────────────────────────────────────
    if not manifest_data.get("image"):
        raise HTTPException(status_code=422, detail="Manifest must have an 'image' field.")
    if not manifest_data.get("key"):
        raise HTTPException(status_code=422, detail="Manifest must have a 'key' field.")

    # ── Sanitize key (rule: install-github-key-sanitization) ─────────────
    raw_key = str(manifest_data["key"])
    key = _re.sub(r"[^a-z0-9_]", "_", raw_key.lower().strip())[:64]
    if not key:
        raise HTTPException(
            status_code=422, detail=f"Manifest key '{raw_key}' is not a valid app key."
        )

    # ── Extract web_port ─────────────────────────────────────────────────
    web_port = manifest_data.get("web_port")
    if not web_port and isinstance(manifest_data.get("ports"), dict):
        web_port = manifest_data["ports"].get("web") or manifest_data["ports"].get("http")

    # ── Build complete manifest (rule: install-custom-complete-manifest) ──
    normalized = {
        "key": key,
        "display_name": manifest_data.get("display_name", key.replace("_", " ").title()),
        "description": manifest_data.get("description", ""),
        "category": manifest_data.get("category", "tools"),
        "tier": manifest_data.get("tier", 2),
        "service_type": manifest_data.get("service_type", "management"),
        "linuxserver": manifest_data.get("linuxserver", True),
        "image": manifest_data.get("image"),
        "image_tag": manifest_data.get("image_tag", "latest"),
        "start_grace_s": manifest_data.get("start_grace_s", 60),
        "ports": manifest_data.get("ports", {"web": web_port} if web_port else {}),
        "volumes": manifest_data.get("volumes", {"config": "/config"}),
        # Traefik: enable by default so custom apps get HTTPS routing
        "traefik": manifest_data.get(
            "traefik",
            {
                "enabled": bool(web_port),
                "subdomain": key,
            },
        ),
        # Health: basic HTTP check if web port available
        "health": manifest_data.get(
            "health",
            {
                "checks": [
                    {
                        "name": "api_reachable",
                        "type": "http",
                        "path": "/",
                        "expect_status": 200,
                        "interval": 60,
                    }
                ]
                if web_port
                else []
            },
        ),
        "tags": manifest_data.get("tags", ["custom"]),
        "source": "community",
    }
    if source_url:
        normalized["source_url"] = source_url

    # ── Preserve app-specific env vars ───────────────────────────────────
    # Copy env vars from the custom manifest, excluding SLOP-managed vars
    # (PUID, PGID, TZ, DOMAIN, etc.) which are always set from the platform
    # DB at install time — the user should never need to override them.
    # These non-managed vars are written to the compose fragment's environment
    # block so custom apps receive their required config at runtime, even if
    # the user left them blank in the variable-discovery form.
    _raw_env = manifest_data.get("env") or {}
    _app_env = {str(k): str(v) for k, v in _raw_env.items() if k not in _SLOP_MANAGED_VARS}
    if _app_env:
        normalized["env"] = _app_env

    # Validate category against the loader's enum
    from backend.manifests.loader import VALID_CATEGORIES

    if normalized["category"] not in VALID_CATEGORIES:
        # Silently remap unknown categories rather than rejecting
        normalized["category"] = "tools"

    # ── Persist ───────────────────────────────────────────────────────────
    community_dir = _cfg.catalog_dir / "community"
    community_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = community_dir / f"{key}.yaml"
    manifest_path.write_text(_yaml.dump(normalized, default_flow_style=False), encoding="utf-8")

    if compose_yaml.strip():
        compose_path = community_dir / f"{key}.compose.yaml"
        compose_path.write_text(compose_yaml, encoding="utf-8")

    return {
        "ok": True,
        "key": key,
        "message": f"App '{key}' registered in community catalog.",
        "install_url": f"/api/apps/{key}/install",
    }


# ── GitHub repo manifest install ──────────────────────────────────────────


class GitHubManifestRequest(BaseModel):
    repo_url: str = Field(
        ...,
        description="GitHub URL to a raw manifest YAML or a repo containing a manifest. "
        "Formats accepted: "
        "https://github.com/user/repo/blob/main/manifest.yaml, "
        "https://raw.githubusercontent.com/user/repo/main/manifest.yaml, "
        "https://github.com/user/repo (scans for manifest.yaml at root)",
    )


@router.post("/install-from-github")
def install_from_github(req: GitHubManifestRequest) -> dict[str, Any]:
    """Fetch a SLOP manifest from a GitHub URL and install the app.

    Accepts:
    - Direct link to a .yaml manifest file
    - GitHub repo URL (scans root for manifest.yaml / slop.yaml)

    Security: only fetches from github.com / raw.githubusercontent.com.
    Manifest is validated before install (required fields, image format, etc.)
    """
    import urllib.error as _err
    import yaml as _yaml

    url = req.repo_url.strip()

    # Validate via shared SSRF guard: exact-host match (github.com.evil.com or a
    # path-embedded token can never pass) + private-IP block. Re-checked after each
    # rewrite below, before any urlopen.
    from backend.core.url_guard import (
        GITHUB_HOSTS,
        UrlNotAllowed,
        assert_allowed_url,
        is_allowed_url,
        pinned_urlopen,
    )

    def _require_github(u: str) -> None:
        try:
            assert_allowed_url(u, allowed_hosts=GITHUB_HOSTS)
        except UrlNotAllowed as exc:
            raise HTTPException(
                status_code=422,
                detail=safe_detail(exc, "Only GitHub https URLs accepted.", log=log),
            ) from exc

    _require_github(url)

    # Convert GitHub blob URL → raw URL
    if "github.com" in url and "/blob/" in url:
        url = url.replace("github.com", "raw.githubusercontent.com").replace("/blob/", "/")
    elif "github.com" in url and "/blob/" not in url and not url.endswith(".yaml"):
        # Repo root — try common manifest names
        base = url.rstrip("/")
        for candidate in ("manifest.yaml", "slop.yaml", "slop-manifest.yaml"):
            raw = base.replace("github.com", "raw.githubusercontent.com") + f"/main/{candidate}"
            if not is_allowed_url(raw, allowed_hosts=GITHUB_HOSTS):  # re-validate rewrite
                continue
            try:
                pinned_urlopen(raw, timeout=5).close()  # SSRF seam: host re-validated + IP-pinned
                url = raw
                break
            except Exception:  # noqa: S112  # probe each candidate; move on if unavailable
                continue
        else:
            raise HTTPException(
                status_code=404,
                detail="No manifest file found at repo root. Expected: manifest.yaml or slop.yaml",
            )

    _require_github(url)  # re-validate the final (possibly rewritten) URL
    try:
        with pinned_urlopen(url, timeout=15) as resp:  # SSRF seam: host re-validated + IP-pinned
            raw_content = resp.read().decode("utf-8")
    except _err.HTTPError as e:
        raise HTTPException(
            status_code=502,
            detail=safe_detail(
                e, "GitHub returned an error response while fetching the manifest.", log=log
            ),
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=502, detail=safe_detail(e, "Could not fetch manifest.", log=log)
        ) from e

    # Parse YAML
    try:
        manifest_data = _yaml.safe_load(raw_content)
    except _yaml.YAMLError as e:
        raise HTTPException(
            status_code=422, detail=safe_detail(e, "Invalid YAML in manifest.", log=log)
        ) from e

    if not isinstance(manifest_data, dict):
        raise HTTPException(status_code=422, detail="Manifest must be a YAML mapping.")

    # Basic validation
    missing = [f for f in ("key", "image") if not manifest_data.get(f)]
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Manifest missing required fields: {', '.join(missing)}",
        )

    # Size guard — prevent malicious huge manifests
    if len(raw_content) > 64_000:
        raise HTTPException(status_code=422, detail="Manifest file exceeds 64 KB size limit.")

    # Sanitize key — must be a safe filesystem/DB identifier
    import re as _re

    raw_key = str(manifest_data["key"])
    app_key = _re.sub(r"[^a-z0-9_]", "_", raw_key.lower().strip())[:64]
    if not app_key:
        raise HTTPException(
            status_code=422, detail=f"Manifest key '{raw_key}' is not a valid app key."
        )
    if app_key != raw_key:
        manifest_data["key"] = app_key  # normalise

    # Delegate to shared normalizer — applies all compliance rules
    result = _save_community_manifest(manifest_data, source_url=url)
    result["message"] = (
        f"Manifest for '{result['key']}' fetched from GitHub and saved to community catalog. "
        f"Use POST /api/apps/{result['key']}/install to install it."
    )
    result["source_url"] = url

    # ── Variable scan ─────────────────────────────────────────────────────
    # Scan the manifest env: block for ${VAR} refs that are not already in
    # the platform .env. Returns missing_vars so the frontend can show a
    # var form before triggering the install (same pattern as paste YAML path).
    env_block = manifest_data.get("env") or {}
    if isinstance(env_block, dict):
        env_values_str = " ".join(str(v) for v in env_block.values())
    else:
        env_values_str = ""
    # Also scan image/ports/volumes fields for any stray ${VAR} refs
    for _field in ("volumes", "image", "image_tag"):
        _val = manifest_data.get(_field, "")
        env_values_str += " " + str(_val)
    required_refs = set(_re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", env_values_str))
    known_keys = _read_env_keys()
    missing_vars = sorted(
        v for v in required_refs if v not in known_keys and v not in _SLOP_MANAGED_VARS
    )
    result["missing_vars"] = missing_vars

    return result


# ── Custom app install from YAML manifest data ──────────────────────────────


class CustomManifestInstall(BaseModel):
    manifest: dict[str, Any]
    compose_yaml: str


@router.post("/install-custom")
def install_custom_app(req: CustomManifestInstall) -> dict[str, Any]:
    """Install a custom app from a validated manifest dict and compose YAML.

    Called by the YAML linter UI after validation succeeds.
    Saves the manifest to the community catalog and queues an install.
    """

    # Delegate entirely to the shared normalizer — single path for all compliance rules
    result = _save_community_manifest(
        manifest_data=req.manifest,
        compose_yaml=req.compose_yaml,
    )
    result["message"] = f"Custom app '{result['key']}' registered. Use the install_url to deploy."
    return result


# ── App version pinning + update ───────────────────────────────────────────


class PinVersionRequest(BaseModel):
    image_tag: str = Field(..., description="Tag to pin, e.g. '4.0.9', 'latest'")


@router.put("/{key}/pin-version")
def pin_app_version(key: str, req: PinVersionRequest) -> dict[str, Any]:
    """Pin an app to a specific image tag.

    Pinned tag is used on the next install or update.
    Set to 'latest' to un-pin and always use latest.
    """
    with StateDB() as db:
        app = db.get_app(key)
    if not app:
        raise HTTPException(404, f"App '{key}' is not installed.")
    tag = req.image_tag.strip()
    if not tag:
        raise HTTPException(422, "image_tag cannot be empty.")
    with StateDB() as db:
        db.upsert_app(key, image_tag=tag)
    return {"ok": True, "key": key, "image_tag": tag}


@router.post("/{key}/update")
def update_app(key: str) -> dict[str, Any]:
    """Pull the latest (or pinned) image and recreate the container.

    This is the 'Update' button — pulls the image then does compose up --force-recreate.
    Non-blocking: check progress via GET /apps/{key}/install/progress.
    """
    key = _safe_key(key)  # #1103: validate before {key} -> compose-fragment path
    import threading
    from backend.manifests.executor import install_app

    with StateDB() as db:
        app = db.get_app(key)
    if not app:
        raise HTTPException(404, f"App '{key}' is not installed.")

    with _install_lock:
        if key in _installing:
            raise HTTPException(409, f"'{key}' is already being updated.")
        # Reserve the slot before starting the thread so no concurrent request
        # can sneak through the window between here and _installing.add(key).
        _installing.add(key)

    def _do_update() -> None:
        try:
            with StateDB() as db:
                db.clear_op_steps(key)
                db.write_op_step(
                    key,
                    "update",
                    "running",
                    f"Pulling {'pinned ' + app.image_tag if app.image_tag != 'latest' else 'latest'} image…",
                )

            # Pull new image via compose up --pull always
            import subprocess
            from backend.core.config import config as _cfg

            frag_path = _cfg.compose_dir / f"{key}.yaml"
            if frag_path.exists():
                r = subprocess.run(
                    [
                        "docker",
                        "compose",
                        "-f",
                        str(frag_path),
                        "--env-file",
                        str(_cfg.env_file),
                        "up",
                        "-d",
                        "--pull",
                        "always",
                        "--force-recreate",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                if r.returncode != 0:
                    with StateDB() as db:
                        db.write_op_step(
                            key, "__done__", "error", f"Update failed: {r.stderr.strip()[:300]}"
                        )
                    return
            else:
                # No fragment — do a fresh install; respect the semaphore to
                # avoid overwhelming the Docker daemon (same cap as batch install).
                with _INSTALL_SEMAPHORE:
                    result = install_app(key)
                if not result.ok:
                    with StateDB() as db:
                        db.write_op_step(
                            key, "__done__", "error", result.error or "Install failed."
                        )
                    return

            with StateDB() as db:
                db.write_op_step(key, "__done__", "ok", "Updated successfully.")
                db.upsert_app(key, status="running")
        except Exception as e:
            with StateDB() as db:
                db.write_op_step(key, "__done__", "error", str(e))
        finally:
            _installing.discard(key)

    threading.Thread(target=_do_update, daemon=True).start()
    return {
        "ok": True,
        "key": key,
        "message": f"Update started. Poll /apps/{key}/install/progress.",
    }


# ── Per-app configuration (config_schema driven) ────────────────────────────


@router.get("/{key}/config")
def get_app_config(key: str) -> dict[str, Any]:
    """Return current config values for an app (driven by manifest config_schema).

    Returns: {schema: [...fields...], values: {key: value}, config_file: path}
    """
    key = _safe_key(key)  # #1103: validate before {key} -> app_configs/{key} path
    from backend.manifests.loader import load_manifest, ManifestError

    try:
        manifest = load_manifest(key)
    except (KeyError, ManifestError) as e:
        raise HTTPException(404, f"No app '{key}' in catalog.") from e

    if not manifest.config_schema:
        return {"schema": [], "values": {}, "config_file": None}

    # Read current config file if it exists
    from backend.core.config import config as _cfg

    app_config_dir = _cfg.data_dir / "app_configs" / key
    config_file = app_config_dir / "config.yml"
    values: dict[str, Any] = dict(manifest.config_defaults)

    if config_file.exists():
        try:
            import yaml as _yaml

            loaded = _yaml.safe_load(config_file.read_text())
            if isinstance(loaded, dict):
                values.update(loaded)
        except Exception:  # noqa: S110  # best-effort config load; manifest defaults used if file is unreadable
            pass

    return {
        "schema": manifest.config_schema,
        "values": values,
        "config_file": str(config_file) if config_file.exists() else None,
    }


class AppConfigUpdate(BaseModel):
    values: dict[str, Any]


@router.put("/{key}/config")
def update_app_config(key: str, req: AppConfigUpdate) -> dict[str, Any]:
    """Write per-app configuration and restart the container.

    Writes to {config_path}/config.json and restarts the container.
    Used for apps with config_schema fields (e.g. DDNS Updater providers list).
    """
    import json as _json
    import subprocess as _sp
    from pathlib import Path

    with StateDB() as db:
        app = db.get_app(key)
    if not app:
        raise HTTPException(status_code=404, detail=f"App '{key}' is not installed.")
    if not app.config_path:
        raise HTTPException(
            status_code=422, detail=f"App '{key}' has no config path — cannot write config."
        )

    config_dir = Path(app.config_path)
    config_dir.mkdir(parents=True, exist_ok=True)
    config_file = config_dir / "config.json"
    config_file.write_text(_json.dumps(req.values, indent=2))

    # Restart the container to pick up new config
    try:
        _sp.run(["docker", "restart", app.container_name or key], capture_output=True, timeout=30)
    except (FileNotFoundError, _sp.TimeoutExpired):
        pass  # Docker not available — user will need to restart manually

    return {
        "ok": True,
        "message": f"Configuration saved for '{key}'. Container restarted.",
        "config_path": str(config_file),
    }


@router.post("/{key}/pull")
def pull_app(key: str) -> dict[str, Any]:
    """Alias for POST /{key}/update — pull latest image and recreate container."""
    return update_app(key)


@router.post("/{key}/pin-tag")
def pin_tag_alias(key: str, req: PinVersionRequest) -> dict[str, Any]:
    """Alias for PUT /{key}/pin-version using POST + tag field."""
    return pin_app_version(key, req)


# ── Post-install guidance steps ───────────────────────────────────────────


@router.get("/{key}/post-install-steps")
def get_post_install_steps(key: str) -> list[dict[str, Any]]:
    """Return guided post-install steps for an app.

    Steps are auto-generated based on app category and type.
    arr apps get indexer/download client guidance.
    DDNS Updater gets provider config guidance.
    """
    from backend.manifests.loader import load_manifest, ManifestError

    key = _safe_key(
        key
    )  # #1167: validate before {key} -> manifest path (uncaught PathNotAllowed -> 500)
    try:
        manifest = load_manifest(key)
    except (KeyError, ManifestError) as e:
        raise HTTPException(404, f"No app '{key}' in catalog.") from e

    steps: list[dict[str, Any]] = []
    category = getattr(manifest, "category", "")
    web_port = getattr(manifest, "web_port", None)

    if category == "arr":
        steps.append(
            {
                "title": "Configure indexers",
                "description": "Add Prowlarr as your indexer source: Settings → Indexers → Add Indexer → Prowlarr.",
                "link": f"http://localhost:{web_port}/Settings/Indexers" if web_port else None,
                "required": True,
            }
        )
        steps.append(
            {
                "title": "Configure download client",
                "description": "Add qBittorrent or SABnzbd: Settings → Download Clients → Add.",
                "link": f"http://localhost:{web_port}/Settings/DownloadClients"
                if web_port
                else None,
                "required": True,
            }
        )
        steps.append(
            {
                "title": "Add media root folder",
                "description": "Set your media library path: Settings → Media Management → Root Folders.",
                "link": f"http://localhost:{web_port}/Settings/MediaManagement"
                if web_port
                else None,
                "required": True,
            }
        )

    elif key == "prowlarr":
        steps.append(
            {
                "title": "Add indexers",
                "description": "Browse and add indexers: Indexers → Add Indexer.",
                "link": f"http://localhost:{web_port}/Indexers/Add" if web_port else None,
                "required": True,
            }
        )
        steps.append(
            {
                "title": "Connect to arr apps",
                "description": "Sync to Sonarr/Radarr: Settings → Apps → Add Application.",
                "link": f"http://localhost:{web_port}/Settings/Apps" if web_port else None,
                "required": True,
            }
        )

    elif key == "ddns_updater":
        steps.append(
            {
                "title": "Configure DNS providers",
                "description": "Open the Configuration tab on this app page and add your DNS provider credentials.",
                "link": None,
                "required": True,
            }
        )
        steps.append(
            {
                "title": "Set DNS records to DNS-only",
                "description": "In Cloudflare: disable the orange proxy cloud on your media subdomain (required for direct streaming).",
                "link": "https://dash.cloudflare.com",
                "required": True,
            }
        )

    elif key in ("plex", "jellyfin", "emby"):
        steps.append(
            {
                "title": "Add media library",
                "description": "Open the app and add your media folder as a library during initial setup.",
                "link": f"http://localhost:{web_port}" if web_port else None,
                "required": True,
            }
        )
        if key == "plex":
            steps.append(
                {
                    "title": "Sign in to Plex",
                    "description": "Claim your server by signing in with your Plex account.",
                    "link": "https://app.plex.tv/desktop/#!/setup",
                    "required": True,
                }
            )

    elif category == "monitoring":
        steps.append(
            {
                "title": "Configure data sources",
                "description": "Add Prometheus, Loki, or other data sources in the app settings.",
                "link": f"http://localhost:{web_port}" if web_port else None,
                "required": False,
            }
        )

    if not steps:
        steps.append(
            {
                "title": "Open the app",
                "description": f"Visit http://localhost:{web_port} to complete setup."
                if web_port
                else "Complete the initial setup in the app.",
                "link": f"http://localhost:{web_port}" if web_port else None,
                "required": False,
            }
        )

    return steps


@router.get("/{key}/check-update")
def check_app_update(key: str) -> dict[str, Any]:
    """Trigger a background update-check for an installed app.

    Returns immediately with {checking: true} if no cached result exists, or
    the cached result if it's less than 5 minutes old (skips re-check).
    Poll GET /{key}/update/result for the actual result.

    docker manifest inspect has a 30s timeout — running it synchronously in the
    API route blocks a worker thread for that duration.
    """
    from backend.core.state import StateDB

    with StateDB() as db:
        app = db.get_app(key)
    if not app:
        raise HTTPException(404, f"App '{key}' is not installed.")

    cached = _update_check_cache.get(key)
    if cached and _time.monotonic() - cached["ts"] < 300:
        # Return fresh cached result without starting a new check
        cached_result = _get_cached_update_result(key)
        if cached_result is not None:
            return cached_result

    image_tag = app.image_tag or "latest"
    image_ref = f"{app.image}:{image_tag}"

    def _run_check() -> None:
        try:
            # Get local image digest
            local = subprocess.run(
                ["docker", "inspect", "--format", "{{.RepoDigests}}", image_ref],
                capture_output=True,
                text=True,
                timeout=10,
            )
            local_digest = local.stdout.strip()

            # Pull manifest digest without downloading
            manifest = subprocess.run(
                ["docker", "manifest", "inspect", "--verbose", image_ref],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if manifest.returncode != 0:
                result: dict[str, Any] = {
                    "update_available": None,
                    "image_ref": image_ref,
                    "note": "Could not reach registry to check for updates.",
                }
            else:
                data = json.loads(manifest.stdout)
                remote_digest = None
                if isinstance(data, list) and data:
                    remote_digest = data[0].get("Descriptor", {}).get("digest")
                elif isinstance(data, dict):
                    remote_digest = data.get("Descriptor", {}).get("digest")

                update_available = bool(
                    remote_digest and local_digest and remote_digest not in local_digest
                )
                result = {
                    "update_available": update_available,
                    "image_ref": image_ref,
                    "local_digest": local_digest or None,
                    "remote_digest": remote_digest,
                }
        except FileNotFoundError:
            result = {
                "update_available": None,
                "image_ref": image_ref,
                "note": "Docker not available on this system.",
            }
        except Exception as e:
            result = {"update_available": None, "image_ref": image_ref, "note": str(e)[:200]}
        _update_check_cache[key] = {"result": result, "ts": _time.monotonic()}

    t = _threading.Thread(target=_run_check, daemon=True)
    t.start()
    return {"checking": True}


@router.get("/{key}/update/result")
def get_update_result(key: str) -> dict[str, Any]:
    """Return the cached update-check result for an app.

    Returns the result from GET /{key}/check-update once the background check
    completes, or {status: pending} if the check is still running.
    """
    cached_result = _get_cached_update_result(key)
    if cached_result is None:
        return {"status": "pending"}
    return cached_result


@router.get("/{key}/health-config")
def get_health_config(key: str) -> dict[str, Any]:
    """Return the health check configuration and current status for an app.

    Works for both catalog apps (official manifests) and custom/community apps.
    Custom apps get an auto-generated HTTP check when a web_port is available.

    Returns:
      key            — app key
      has_manifest   — whether a manifest was found (catalog or community)
      is_community   — whether this is a custom/community-installed app
      checks_defined — number of health check definitions in the manifest
      checks         — list of health check definitions
      current_status — list of most-recent health check records from DB
    """
    from backend.manifests.loader import load_manifest, ManifestError
    from backend.core.state import StateDB

    key = _safe_key(
        key
    )  # #1167: validate before {key} -> manifest path (uncaught PathNotAllowed -> 500)
    # Try to load manifest — works for both official and community apps
    manifest = None
    is_community = False
    try:
        manifest = load_manifest(key)
        # Community manifests are saved with source="community" in YAML;
        # detect by checking the source_path location.
        if manifest.source_path is not None:
            is_community = "community" in str(manifest.source_path)
    except (KeyError, ManifestError):
        pass

    checks: list[dict[str, Any]] = []
    if manifest:
        for hc in manifest.health_checks:
            checks.append(
                {
                    "name": hc.name,
                    "type": hc.check_type,
                    "path": hc.path,
                    "expect_status": hc.expect_status,
                    "interval": hc.interval,
                    "port": hc.port,
                }
            )

    with StateDB() as db:
        db_checks = db.get_health_checks("app", key)
    current_status = [
        {
            "check_name": c.check_name,
            "status": c.status,
            "summary": c.summary,
            "detail": c.detail,
            "checked_at": c.checked_at,
        }
        for c in db_checks
    ]

    return {
        "key": key,
        "has_manifest": manifest is not None,
        "is_community": is_community,
        "checks_defined": len(checks),
        "checks": checks,
        "current_status": current_status,
    }


class HealthPathRequest(BaseModel):
    """Typed body for POST /{key}/probe-path."""

    path: str = Field("/health", description="HTTP path to probe on the app's host port")


@router.post("/{key}/probe-path")
async def probe_health_path(key: str, req: HealthPathRequest) -> dict[str, Any]:
    """Test if a custom app responds to an HTTP health check path."""
    from backend.core.url_guard_httpx import pinned_async_client
    from backend.core.state import StateDB

    from urllib.parse import urlparse

    with StateDB() as db:
        app = db.get_app(key)
    if not app or not app.host_port:
        raise HTTPException(status_code=404, detail="App not found or no port configured")
    path = req.path
    url = f"http://localhost:{app.host_port}{path}"
    # SSRF guard: req.path is user-controlled and interpolated raw into the authority's
    # tail. A crafted path ("@evil.com/", "//evil.com/") re-points the fetch to an
    # arbitrary host via userinfo/scheme-relative injection. Re-parse the URL we are about
    # to fetch and require it still targets the app's own localhost port (#1194).
    try:
        _parsed = urlparse(url)
        _authority_ok = _parsed.hostname == "localhost" and _parsed.port == app.host_port
    except ValueError:
        _authority_ok = False
    if not _authority_ok:
        raise HTTPException(
            status_code=422,
            detail="Invalid probe path — it must be a path on the app's local port.",
        )
    try:
        async with pinned_async_client(timeout=5) as client:
            r = await client.get(url)
        return {"reachable": True, "status": r.status_code, "path": path}
    except Exception as e:
        return {
            "reachable": False,
            "status": None,
            "path": path,
            "error": safe_detail(e, "Reachability check failed.", log=log),
        }


class EnhanceRequest(BaseModel):
    health_path: str = "/health"
    start_grace_s: int = 60
    category: str = "tools"
    display_name: str = ""


@router.post("/{key}/enhance")
def enhance_custom_app(key: str, req: EnhanceRequest) -> dict[str, Any]:
    """Promote a custom app to full monitoring by writing a minimal manifest."""
    key = _safe_key(key)  # #1103: validate before {key} -> community manifest path
    from backend.core.state import StateDB
    from backend.core.config import config as cfg
    import yaml as _yaml

    with StateDB() as db:
        app = db.get_app(key)

    if not app:
        raise HTTPException(status_code=404, detail="App not found")

    # Build a minimal manifest YAML for this custom app
    manifest_content = {
        "key": key,
        "display_name": req.display_name or app.display_name,
        "description": "Custom app managed by SLOP",
        "category": req.category,
        "service_type": "custom",
        "tier": 3,
        "image": app.image or "unknown",
        "image_tag": app.image_tag or "latest",
        "web_port": app.web_port,
        "start_grace_s": req.start_grace_s,
        "health": {
            "checks": [
                {
                    "name": "http_reachable",
                    "type": "http",
                    "path": req.health_path,
                    "interval": 60,
                }
            ]
        },
    }

    # Write to the community catalog directory — the same location that
    # _save_community_manifest() uses, so load_manifest() and the health
    # cycle can find the updated manifest immediately.
    # (Previously wrote to catalog/custom/, which load_manifest() never scanned.)
    community_dir = cfg.catalog_dir / "community"
    community_dir.mkdir(exist_ok=True)
    manifest_path = community_dir / f"{key}.yaml"

    try:
        with open(manifest_path, "w") as f:
            _yaml.dump(manifest_content, f, default_flow_style=False, allow_unicode=True)
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=safe_detail(e, "Could not write manifest.", log=log)
        ) from e

    # Invalidate the manifest cache so the health cycle picks up the new
    # health checks on the next run rather than using the 5-minute-old cache.
    from backend.manifests.loader import clear_cache as _clear_manifest_cache

    _clear_manifest_cache()

    # Update app record
    with StateDB() as db:
        db.upsert_app(
            key,
            display_name=req.display_name or app.display_name,
            category=req.category,
            manifest_source="custom_enhanced",
        )

    return {
        "ok": True,
        "message": (
            f"Monitoring enhanced. {app.display_name} now has HTTP health checks "
            f"on {req.health_path} with {req.start_grace_s}s grace period."
        ),
        "manifest_path": str(manifest_path),
    }
