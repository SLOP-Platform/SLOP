"""backend/agent/verify.py

Post-fix container health verification.

Poll docker state after a fix action to confirm the container actually
became healthy before the caller records success.
"""

from __future__ import annotations

import time

from backend.core.docker_client import DockerError, get_container
from backend.core.logging import get_logger

log = get_logger(__name__)


def verify_container_healthy(
    app_key: str, *, attempts: int = 5, interval_s: float = 3.0
) -> tuple[bool, str]:
    """Poll docker state after a fix. Returns (healthy, summary).

    healthy=True iff container status == 'running' AND
    (no healthcheck defined OR health == 'healthy').
    Never raises; on docker error returns (False, <reason>).
    """
    last_status = "unknown"
    last_health = "unknown"

    for attempt in range(1, attempts + 1):
        try:
            info = get_container(app_key)
        except DockerError as exc:
            reason = f"docker error on attempt {attempt}/{attempts}: {exc}"
            log.warning("verify_container_healthy(%s): %s", app_key, reason)
            return False, reason
        except Exception as exc:  # pragma: no cover — safety net
            reason = f"unexpected error on attempt {attempt}/{attempts}: {exc}"
            log.warning("verify_container_healthy(%s): %s", app_key, reason)
            return False, reason

        if info is None:
            last_status = "not_found"
            last_health = "none"
        else:
            last_status = info.status
            last_health = info.health

            if last_status == "running":
                # No healthcheck defined → health == "none"; treat as healthy
                if last_health in ("healthy", "none"):
                    summary = (
                        f"container running, health={last_health} "
                        f"(confirmed on attempt {attempt}/{attempts})"
                    )
                    log.info("verify_container_healthy(%s): %s", app_key, summary)
                    return True, summary

        if attempt < attempts:
            time.sleep(interval_s)

    summary = f"not healthy after {attempts} attempts; status={last_status}, health={last_health}"
    log.warning("verify_container_healthy(%s): %s", app_key, summary)
    return False, summary
