"""backend/health/probe_backoff.py

Per-probe failure bookkeeping + thread-leak backoff for the post-cycle ambient
probes (#825 / #1144).

`scheduler._pt` bounds each sync probe with `asyncio.wait_for`, but asyncio
cannot KILL the orphaned worker thread a hung probe parks — it stays blocked
until its (timeout-less) call returns. So a *chronically* hung probe leaks one
worker thread per cycle; once the default thread pool is exhausted (~min(32,
cpu+4)), `asyncio.to_thread` itself blocks and every probe re-stalls — the exact
failure #825's per-probe timeout was meant to prevent, reintroduced over time.

This module closes that gap (the deferred remainder of #825, tracked as #1144):
it counts *consecutive timeouts* per probe and, after
``PROBE_BACKOFF_THRESHOLD`` of them, SKIPS the probe for ``PROBE_BACKOFF_CYCLES``
cycles (no dispatch → no new parked thread), then re-tries it once. A fresh
timeout re-arms the backoff; any responsive result (success OR a non-timeout
exception — the thread returned, so it is not hanging) clears the timeout
streak.

It also owns the pre-existing consecutive-failure warn+persist behavior so the
scheduler's post-cycle loop stays a thin dispatcher.
"""

from __future__ import annotations

import asyncio
from typing import Any

from backend.core.logging import get_logger
from backend.health.swallow_counter import record_swallow

log = get_logger(__name__)

# Consecutive timeouts before a probe is parked in a backoff window.
PROBE_BACKOFF_THRESHOLD = 3
# Cycles a backed-off probe is skipped before one retry trial.
PROBE_BACKOFF_CYCLES = 10
# Consecutive failures (any kind) before we warn + persist (pre-#1144 behavior).
PROBE_PERSIST_THRESHOLD = 5

# Module state — keyed by probe name. Survives across cycles for the lifetime of
# the scheduler process (the counters ARE the cross-cycle memory).
_fail_counts: dict[str, int] = {}
_timeout_counts: dict[str, int] = {}
_skip_remaining: dict[str, int] = {}


def _is_timeout(exc: BaseException) -> bool:
    # asyncio.wait_for raises asyncio.TimeoutError (an alias of builtin
    # TimeoutError on 3.11+); accept both so the classification is version-proof.
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError))


def due_for_dispatch(name: str) -> bool:
    """Whether ``name`` should run this cycle, decrementing any backoff window.

    A probe in a thread-leak backoff window is skipped (returns ``False``) and
    its remaining-skip counter is decremented; the cycle it reaches zero the
    probe is eligible again for one retry trial.
    """
    remaining = _skip_remaining.get(name, 0)
    if remaining <= 0:
        return True
    remaining -= 1
    if remaining <= 0:
        _skip_remaining.pop(name, None)
    else:
        _skip_remaining[name] = remaining
    return False


def record_result(name: str, result: Any) -> None:
    """Update counters for one post-cycle probe result + arm/clear backoff.

    ``result`` is the value (or Exception, under ``return_exceptions=True``)
    returned by the probe's awaitable. A ``TimeoutError`` drives the thread-leak
    backoff; any other exception is a normal failure (the worker thread
    returned); a non-exception is success.
    """
    if not isinstance(result, Exception):
        _fail_counts.pop(name, None)
        _timeout_counts.pop(name, None)
        _skip_remaining.pop(name, None)
        return

    _fail_counts[name] = _fail_counts.get(name, 0) + 1
    count = _fail_counts[name]

    if _is_timeout(result):
        _timeout_counts[name] = _timeout_counts.get(name, 0) + 1
        if _timeout_counts[name] >= PROBE_BACKOFF_THRESHOLD and _skip_remaining.get(name, 0) == 0:
            _skip_remaining[name] = PROBE_BACKOFF_CYCLES
            log.warning(
                "Probe '%s' timed out %d consecutive cycles — backing off "
                "(skipping %d cycles) to avoid worker-thread leak (#1144).",
                name,
                _timeout_counts[name],
                PROBE_BACKOFF_CYCLES,
            )
    else:
        # The worker thread RETURNED (raised) — not a hang — so the timeout
        # streak (and its thread-leak risk) resets even though the probe failed.
        _timeout_counts.pop(name, None)

    if count >= PROBE_PERSIST_THRESHOLD:
        log.warning("Probe '%s' has failed %d consecutive times: %s", name, count, result)
        try:
            from backend.core.state import StateDB

            with StateDB() as db:
                db.write_probe_failure(name, count, str(result))
        except Exception:  # best-effort DB write; counter stays in memory if DB unavailable
            record_swallow("scheduler.probe_failure_db_write")


def is_backed_off(name: str) -> bool:
    """True if ``name`` is currently parked in a backoff window (test/inspection)."""
    return _skip_remaining.get(name, 0) > 0


def fail_count(name: str) -> int:
    """Current consecutive-failure count for ``name`` (test/inspection)."""
    return _fail_counts.get(name, 0)


def reset() -> None:
    """Clear all probe state (test helper / scheduler restart)."""
    _fail_counts.clear()
    _timeout_counts.clear()
    _skip_remaining.clear()


__all__ = [
    "PROBE_BACKOFF_CYCLES",
    "PROBE_BACKOFF_THRESHOLD",
    "PROBE_PERSIST_THRESHOLD",
    "due_for_dispatch",
    "fail_count",
    "is_backed_off",
    "record_result",
    "reset",
]
