"""backend/core/docker_client.py

Thin, typed wrapper around the Docker SDK.

Design rules:
  - Never expose raw docker.errors to callers — translate to DockerError
  - All methods are synchronous (FastAPI runs them in a thread pool)
  - One client instance per process, reconnects on socket error
  - Plain-language errors only — no raw Docker daemon messages
"""

from __future__ import annotations

import time as _time
from dataclasses import dataclass
from typing import Any, cast

import docker
import docker.errors

from backend.core.config import config
from backend.core.logging import get_logger

log = get_logger(__name__)


class DockerError(Exception):
    """Plain-language error from a Docker operation."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class ContainerInfo:
    id: str
    name: str
    image: str
    status: str  # running | exited | paused | restarting | dead
    state: str  # docker state enum
    health: str  # healthy | unhealthy | starting | none
    created: int  # unix timestamp


@dataclass
class NetworkInfo:
    id: str
    name: str
    driver: str
    containers: list[str]  # container names attached


# ---------------------------------------------------------------------------
# Client singleton
# ---------------------------------------------------------------------------


_client: docker.DockerClient | None = None
_last_ping_at: float = 0.0
_PING_INTERVAL: float = 30.0  # seconds between liveness pings

# Short-lived container-list cache — avoids redundant Docker API calls when
# list_apps() and the health scheduler both request container status within
# the same second.  TTL of 3s means the dashboard always sees data <3s stale.
_container_cache: list[Any] | None = None
_container_cache_at: float = 0.0
_CONTAINER_CACHE_TTL: float = 3.0  # seconds


def client() -> docker.DockerClient:
    """Return the Docker client, reconnecting if the socket was lost.

    Pings Docker at most once per _PING_INTERVAL seconds (default 30s).
    Previously pinged on every call, which added a full Docker API roundtrip
    to every operation and caused the event loop to block during health cycles.
    """
    global _client, _last_ping_at
    now = _time.monotonic()
    if _client is None:
        _client = _connect()
        _last_ping_at = now
    elif now - _last_ping_at > _PING_INTERVAL:
        # Periodic liveness check — not on every call
        try:
            _client.ping()
            _last_ping_at = now
        except Exception:
            log.warning("Docker socket lost — reconnecting")
            try:
                _client.close()
            except Exception:  # noqa: S110  # best-effort close before reconnect; ignore close errors
                pass
            _client = _connect()
            _last_ping_at = now
    return _client


def _connect() -> docker.DockerClient:
    try:
        c = docker.DockerClient(base_url=config.docker_socket, timeout=10)
        c.ping()
        log.info("Connected to Docker at %s", config.docker_socket)
        return c
    except docker.errors.DockerException as e:
        raise DockerError(
            f"Cannot connect to Docker at {config.docker_socket}. "
            f"Make sure Docker is running and the socket is accessible. "
            f"Detail: {e}"
        ) from e


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------


def list_containers(include_stopped: bool = False) -> list[ContainerInfo]:
    try:
        containers = client().containers.list(all=include_stopped)
        return [_container_info(c) for c in containers]
    except docker.errors.DockerException as e:
        raise DockerError(f"Could not list containers: {e}") from e


def get_container(name: str) -> ContainerInfo | None:
    try:
        c = client().containers.get(name)
        return _container_info(c)
    except docker.errors.NotFound:
        return None
    except docker.errors.DockerException as e:
        raise DockerError(f"Could not get container '{name}': {e}") from e


def _cached_container_list() -> list[Any]:
    """Return containers.list(all=True) with a 3-second cache.

    Avoids duplicate Docker API calls when list_apps() and the health scheduler
    both request container state within the same scheduling window.
    Callers must not mutate the returned list.
    """
    global _container_cache, _container_cache_at
    now = _time.monotonic()
    if _container_cache is not None and (now - _container_cache_at) <= _CONTAINER_CACHE_TTL:
        return _container_cache
    _container_cache = client().containers.list(all=True)
    _container_cache_at = now
    return _container_cache


def get_containers_by_name(names: list[str]) -> dict[str, ContainerInfo]:
    """Fetch container info for a list of names in a single Docker API call.

    Returns {name: ContainerInfo} for containers that exist.  Missing names
    are omitted (not None) — callers should use .get(name) with a fallback.

    Uses _cached_container_list() so multiple callers within 3s share one
    Docker API call instead of each making a separate containers.list().
    """
    if not names:
        return {}
    try:
        all_containers = _cached_container_list()
        name_set = set(names)
        return {c.name: _container_info(c) for c in all_containers if c.name in name_set}
    except (DockerError, Exception):
        return {}  # Docker unavailable — callers fall back to DB status


def container_logs(name: str, tail: int = 100) -> str:
    try:
        c = client().containers.get(name)
        return cast(str, c.logs(tail=tail, timestamps=False).decode("utf-8", errors="replace"))
    except docker.errors.NotFound as e:
        raise DockerError(f"Container '{name}' not found.") from e
    except docker.errors.DockerException as e:
        raise DockerError(f"Could not get logs for '{name}': {e}") from e


def _container_info(c: Any) -> ContainerInfo:
    attrs = c.attrs or {}

    # containers.list() returns State as a string ("running", "exited", …).
    # containers.get() / inspect returns State as a dict with Health etc.
    # Handle both so this function is safe for both call paths.
    raw_state = attrs.get("State", {})
    state: dict[str, Any] = raw_state if isinstance(raw_state, dict) else {}

    health = (state.get("Health") or {}).get("Status", "none")

    created_str = attrs.get("Created", "")
    import datetime

    try:
        created = int(
            datetime.datetime.fromisoformat(created_str.replace("Z", "+00:00")).timestamp()
        )
    except (ValueError, TypeError, AttributeError):
        created = 0

    # Use attrs["Image"] (the image ref string already present in the list/inspect
    # response) instead of c.image.tags — accessing c.image.tags triggers a separate
    # Docker API image-load call for every container, which is the root cause of the
    # ~1s latency on /api/v1/apps with 18 containers.
    image = attrs.get("Image", "") or getattr(c, "short_id", "")

    return ContainerInfo(
        id=c.id[:12],
        name=c.name,
        image=image,
        status=c.status,
        state=state.get("Status", c.status),
        health=health,
        created=created,
    )


# ---------------------------------------------------------------------------
# Networks
# ---------------------------------------------------------------------------


def network_exists(name: str) -> bool:
    try:
        client().networks.get(name)
        return True
    except docker.errors.NotFound:
        return False
    except docker.errors.DockerException as e:
        raise DockerError(f"Could not check network '{name}': {e}") from e


def create_network(name: str, driver: str = "bridge") -> NetworkInfo:
    try:
        if network_exists(name):
            net = client().networks.get(name)
        else:
            net = client().networks.create(name, driver=driver, check_duplicate=True)
            log.info("Created Docker network: %s", name)
        return _network_info(net)
    except docker.errors.DockerException as e:
        raise DockerError(
            f"Could not create network '{name}'. "
            f"Check that no other network uses the same name. Detail: {e}"
        ) from e


def get_network(name: str) -> NetworkInfo | None:
    try:
        net = client().networks.get(name)
        return _network_info(net)
    except docker.errors.NotFound:
        return None
    except docker.errors.DockerException as e:
        raise DockerError(f"Could not get network '{name}': {e}") from e


def _network_info(net: Any) -> NetworkInfo:
    containers = list((net.attrs.get("Containers") or {}).keys())
    return NetworkInfo(
        id=net.id[:12],
        name=net.name,
        driver=net.attrs.get("Driver", "bridge"),
        containers=containers,
    )


# ---------------------------------------------------------------------------
# Port availability
# ---------------------------------------------------------------------------


def ports_in_use() -> dict[int, str]:
    """Return a map of host_port → container_name for all running containers."""
    result: dict[int, str] = {}
    seen: set[tuple[Any, ...]] = set()
    try:
        for c in client().containers.list():
            bindings = (c.attrs.get("NetworkSettings") or {}).get("Ports") or {}
            for _, hosts in (bindings or {}).items():
                if not hosts:
                    continue
                for h in hosts:
                    try:
                        hp = int(h.get("HostPort") or 0)
                        if hp:
                            key = (hp, c.name)
                            if key not in seen:
                                seen.add(key)
                                result[hp] = c.name
                    except (TypeError, ValueError):
                        pass
    except docker.errors.DockerException as e:
        raise DockerError(f"Could not check port usage: {e}") from e
    return result


# ---------------------------------------------------------------------------
# Docker info
# ---------------------------------------------------------------------------


def daemon_info() -> dict[str, Any]:
    try:
        info = client().info()
        return {
            "version": client().version().get("Version", "unknown"),
            "containers": info.get("Containers", 0),
            "containers_running": info.get("ContainersRunning", 0),
            "images": info.get("Images", 0),
            "os": info.get("OperatingSystem", "unknown"),
            "architecture": info.get("Architecture", "unknown"),
        }
    except DockerError:
        raise
    except docker.errors.DockerException as e:
        raise DockerError(f"Could not get Docker info: {e}") from e


def gpu_available() -> dict[str, bool]:
    """Detect available GPU runtimes."""
    result = {"nvidia": False, "amd": False}
    try:
        info = client().info()
        runtimes = info.get("Runtimes", {})
        result["nvidia"] = "nvidia" in runtimes
        # AMD ROCm doesn't register a named runtime — check for /dev/dri
        import os

        result["amd"] = os.path.exists("/dev/dri")
    except Exception:  # noqa: S110  # best-effort GPU detection; return empty result if Docker unavailable
        pass
    return result
