"""backend/core/supervisor.py

Supervised-task primitive — closes the silent-task-death class.

Every ``asyncio.create_task(coro)`` fire-and-forget is a hand-rolled risk: if
the coroutine raises, the task dies silently and the responsibility it owned
(health scheduling, docker event watching, source scanning, startup reconcile)
just stops with no operator-visible signal.

``spawn_supervised`` wraps that pattern with a structural guarantee:
  - On unhandled exception the death is logged at ERROR with the task name and
    full traceback (never swallowed).
  - The death is recorded as an agent health row (``subject_type='agent'``) so
    it surfaces on ``/api/health/agent`` instead of vanishing into the void.
  - Optional bounded auto-restart with exponential-ish backoff. When restart is
    exhausted or disabled, the dead-state health row is left in place.

The primitive is self-contained in core so other modules can import it cleanly
without pulling in scheduler / api state.

Usage::

    from backend.core.supervisor import spawn_supervised, RestartPolicy

    task = spawn_supervised(
        "health-scheduler",
        lambda: _scheduler_loop(),
        restart=RestartPolicy(max_restarts=5),
    )

``coro_factory`` is a *zero-arg callable returning a fresh coroutine* — it is
re-invoked to build a new coroutine on each restart (a coroutine object can
only be awaited once, so a bare coroutine cannot be restarted).
"""

from __future__ import annotations

import asyncio
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from backend.core.logging import get_logger

log = get_logger(__name__)

# Agent health surface — kept local so this module stays independent of
# backend.core.agent (which imports state + reality_view). The string values
# MUST match backend.core.agent.AGENT_SUBJECT_TYPE / AGENT_KEY so the rows land
# on the same /api/health/agent surface.
SUPERVISOR_SUBJECT_TYPE: str = "agent"
SUPERVISOR_SUBJECT_KEY: str = "slop_agent"

# Status values written to the agent health row.
STATUS_OK: str = "ok"
STATUS_ERROR: str = "error"


@dataclass(frozen=True)
class RestartPolicy:
    """Bounded auto-restart configuration for a supervised task.

    Attributes:
        max_restarts: Maximum number of restarts after the initial run. ``0``
            disables restart (run once; record death on failure).
        base_delay: First backoff delay in seconds.
        max_delay: Upper bound on backoff delay in seconds.
        backoff_factor: Multiplier applied to the delay after each restart
            (exponential-ish backoff).
    """

    max_restarts: int = 0
    base_delay: float = 1.0
    max_delay: float = 60.0
    backoff_factor: float = 2.0

    def delay_for(self, restart_index: int) -> float:
        """Backoff delay (seconds) before the ``restart_index``-th restart (1-based)."""
        delay = self.base_delay * (self.backoff_factor ** max(0, restart_index - 1))
        return min(delay, self.max_delay)


# Module-level registry of live supervised tasks. Holding a reference prevents
# the event loop from GC-ing the task (the bug RUF006 warns about) — and lets
# tests / introspection enumerate what is supervised.
_supervised: dict[str, asyncio.Task[None]] = {}


def _check_name(name: str) -> str:
    return f"task:{name}"


def _record_health(name: str, status: str, summary: str, detail: str | None = None) -> None:
    """Upsert an agent health row recording a supervised task's state.

    Best-effort: a DB failure here must never crash the supervisor wrapper (that
    would itself be a silent-death path). Imported lazily so this module has no
    import-time dependency on the DB layer.
    """
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            db.upsert_health_check(
                subject_type=SUPERVISOR_SUBJECT_TYPE,
                subject_key=SUPERVISOR_SUBJECT_KEY,
                check_name=_check_name(name),
                status=status,
                summary=summary,
                detail=detail,
            )
    except Exception as _we:  # pragma: no cover - defensive
        log.warning("Failed to record supervised-task health for %s: %s", name, _we)


async def _supervise(
    name: str,
    coro_factory: Callable[[], Awaitable[Any]],
    restart: RestartPolicy,
) -> None:
    """Run + supervise ``coro_factory`` with logging, health rows, and restart.

    Cancellation propagates (graceful shutdown via ``task.cancel()`` is normal,
    not a death — no error row is written).
    """
    restarts = 0
    while True:
        try:
            await coro_factory()
            # Clean completion — the coroutine returned. For long-lived loops
            # this normally only happens on shutdown; record an ok state.
            _record_health(name, STATUS_OK, f"{name} completed normally")
            return
        except asyncio.CancelledError:
            # Graceful cancellation is not a death — re-raise so the loop stops.
            log.info("Supervised task %s cancelled.", name)
            raise
        except Exception as exc:
            tb = traceback.format_exc()
            log.error("Supervised task %s died: %s\n%s", name, exc, tb)
            if restarts < restart.max_restarts:
                restarts += 1
                delay = restart.delay_for(restarts)
                _record_health(
                    name,
                    STATUS_ERROR,
                    f"{name} crashed ({exc}); restarting "
                    f"({restarts}/{restart.max_restarts}) in {delay:.1f}s",
                    detail=tb,
                )
                try:
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    log.info("Supervised task %s cancelled during restart backoff.", name)
                    raise
                continue
            # Restart exhausted (or disabled) — leave the dead-state row in place.
            _record_health(
                name,
                STATUS_ERROR,
                f"{name} died and will not restart (after {restarts} restart(s)): {exc}",
                detail=tb,
            )
            return


def spawn_supervised(
    name: str,
    coro_factory: Callable[[], Awaitable[Any]],
    *,
    restart: RestartPolicy | None = None,
) -> asyncio.Task[None]:
    """Spawn a supervised background task.

    Args:
        name: Stable identifier for the task (used in logs + the agent health
            row's ``check_name`` as ``task:<name>``).
        coro_factory: Zero-arg callable returning a *fresh* coroutine each call,
            so the task can be rebuilt on restart.
        restart: Optional :class:`RestartPolicy`. ``None`` means run-once (no
            auto-restart); a death is still logged + recorded as a health row.

    Returns:
        The created :class:`asyncio.Task`. A module-level reference is also held
        so the task is never garbage-collected out from under the event loop.
    """
    policy = restart if restart is not None else RestartPolicy()

    # If a prior supervised task under this name is still live, do not double-spawn.
    existing = _supervised.get(name)
    if existing is not None and not existing.done():
        log.debug("Supervised task %s already running.", name)
        return existing

    task = asyncio.create_task(_supervise(name, coro_factory, policy), name=name)
    _supervised[name] = task

    def _cleanup(t: asyncio.Task[None], _name: str = name) -> None:
        # Drop the registry entry once the supervisor wrapper itself finishes.
        if _supervised.get(_name) is t:
            _supervised.pop(_name, None)

    task.add_done_callback(_cleanup)
    return task


def supervised_tasks() -> dict[str, asyncio.Task[None]]:
    """Return a snapshot of currently-registered supervised tasks (for introspection)."""
    return dict(_supervised)
