"""backend/agent/watcher.py

Phase F: Docker event watcher.

Subscribes to `docker events --format json` on the host Docker socket.
Filters for events on slop-managed containers (those whose name
matches the Compose pattern `slop-<app_key>-<replica>`).
On die/oom/health_status=unhealthy events, calls
install_failure_listener() which feeds into the existing agent pipeline.

Starts as a background asyncio task in lifespan (backend/api/main.py).
Cancels cleanly on shutdown.

Phase F extension: install_failure_listener() now also handles runtime
container failures (die, oom, unhealthy) — not just fresh install steps.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from backend.agent.listener import install_failure_listener
from backend.core.logging import get_logger

log = get_logger(__name__)

# Module-level reference to the long-running watcher task.
_watcher_task: asyncio.Task[None] | None = None

_FILTER_ACTIONS = frozenset({"die", "oom", "health_status"})


def _extract_app_key(container_name: str) -> str | None:
    """Extract app_key from a slop Compose container name.

    Expected format: ``slop-<app_key>-<replica>`` (the default
    Compose naming: project=slop, service=<app_key>, replica=1).
    Leading ``/`` is stripped (Docker API sometimes includes it).

    Returns None if the name does not match — the event is ignored.
    """
    m = re.match(r"^/?slop-(.+?)-\d+$", container_name)
    if m:
        return m.group(1)
    return None


async def _handle_event(event: dict[str, Any]) -> None:
    """Process one parsed Docker event dict.

    Filters by action (die/oom/health_status=unhealthy) and by container
    name (must match slop Compose pattern).  On match, builds a
    synthetic step_log and calls install_failure_listener().
    """
    action = event.get("Action", "")
    if action not in _FILTER_ACTIONS:
        return

    attrs = event.get("Actor", {}).get("Attributes", {})

    # health_status events fire for both healthy and unhealthy — ignore healthy.
    if action == "health_status" and attrs.get("health_status") != "unhealthy":
        return

    container_name = attrs.get("name", "")
    app_key = _extract_app_key(container_name)
    if app_key is None:
        return  # not a slop container

    exit_code = attrs.get("exitCode", "unknown")
    step_log = {
        "name": "docker_event",
        "status": "error",
        "message": f"Container {container_name} {action}",
        "detail": f"Docker event: action={action} exitCode={exit_code}",
    }

    await install_failure_listener(app_key, step_log)


async def _watch_loop() -> None:
    """Subscribe to docker events and dispatch matching ones to the pipeline.

    Loops forever.  On subprocess exit (Docker daemon restart), logs a
    warning, waits 5 s, and restarts.  Cancelled cleanly via CancelledError.
    """
    proc: asyncio.subprocess.Process | None = None
    while True:
        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "events",
                "--format",
                "json",
                stdout=asyncio.subprocess.PIPE,
            )
            assert proc.stdout is not None
            async for raw_line in proc.stdout:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                    await _handle_event(event)
                except json.JSONDecodeError:
                    log.debug("Non-JSON docker event line (skipped): %r", line)
                except Exception as exc:
                    log.warning("Error handling docker event: %s", exc)
            # stdout closed: docker daemon exited or restarted
            log.warning("docker events process exited; restarting in 5 s")
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            if proc is not None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
            raise


async def start_docker_event_watcher() -> None:
    """Create the docker event watcher background task.

    Called via ``asyncio.create_task(start_docker_event_watcher())``
    inside the FastAPI lifespan startup block.
    """
    global _watcher_task
    from backend.core.supervisor import RestartPolicy, spawn_supervised

    # Supervised so an unhandled subprocess-spawn failure (e.g. docker binary
    # missing) surfaces as an agent health row instead of dying silently.
    _watcher_task = spawn_supervised(
        "docker-event-watcher",
        lambda: _watch_loop(),
        restart=RestartPolicy(max_restarts=5),
    )
    log.info("Docker event watcher task created.")


async def stop_docker_event_watcher() -> None:
    """Cancel the docker event watcher gracefully.

    Called with ``await stop_docker_event_watcher()`` inside the FastAPI
    lifespan shutdown block.
    """
    global _watcher_task
    if _watcher_task is not None and not _watcher_task.done():
        _watcher_task.cancel()
        log.info("Docker event watcher cancelled.")
    _watcher_task = None
