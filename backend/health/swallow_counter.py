"""backend/health/swallow_counter.py

In-memory swallow counter for observable silent-failure sites.

Convention: every intentionally swallowed exception at a strategic
site MUST call record_swallow(site) so degradation becomes visible
on /api/health/agent without becoming blocking.

Usage:
    from backend.health.swallow_counter import record_swallow
    except Exception:
        record_swallow("scheduler.config_load")

The accumulated counters are served by GET /api/health/agent via
get_swallow_counts(), which returns a dict keyed by site name.

Thread-safety: uses a threading.Lock; safe from both sync and
asyncio-to-thread contexts.
"""

from __future__ import annotations

import threading
import time
from typing import Any

_lock = threading.Lock()
_counts: dict[str, int] = {}
_first_seen: dict[str, int] = {}
_last_seen: dict[str, int] = {}


def record_swallow(site: str) -> None:
    """Increment the in-memory swallow counter for *site*.

    Never raises — calling this must be as safe as doing nothing.
    *site* should be a stable dotted identifier, e.g.
    ``"scheduler.config_load"`` or ``"managed_services.status_write"``.
    """
    try:
        now = int(time.time())
        with _lock:
            _counts[site] = _counts.get(site, 0) + 1
            if site not in _first_seen:
                _first_seen[site] = now
            _last_seen[site] = now
    except Exception:  # noqa: S110  # counter must never raise
        pass


def get_swallow_counts() -> dict[str, Any]:
    """Return a snapshot of all swallow counters.

    Shape:
        {
            "total": <int>,
            "sites": {
                "<site>": {
                    "count": <int>,
                    "first_seen": <unix_ts>,
                    "last_seen": <unix_ts>,
                },
                ...
            }
        }
    """
    with _lock:
        sites = {
            site: {
                "count": _counts[site],
                "first_seen": _first_seen.get(site, 0),
                "last_seen": _last_seen.get(site, 0),
            }
            for site in _counts
        }
    return {
        "total": sum(s["count"] for s in sites.values()),
        "sites": sites,
    }


def reset_counts() -> None:
    """Reset all counters — intended for tests only."""
    with _lock:
        _counts.clear()
        _first_seen.clear()
        _last_seen.clear()
