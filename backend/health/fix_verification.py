"""Post-fix verification — re-check a remediated app after a delay (#996).

``approve_fix`` used to do this in an UNJOINABLE daemon thread that slept 60s then
ran a real ``docker inspect`` + DB write: untestable-by-design, silently lost on a
process restart inside the window, and unobservable. This module runs it as a
SUPERVISED asyncio task (``backend.core.supervisor``) instead: GC-safe, cancellable
on shutdown, its death logged + health-recorded, and the blocking docker/DB work
runs in a worker thread via ``asyncio.to_thread`` so the event loop is never blocked.
The delay is a module constant so a test can shrink it to 0 (the injectable clock
#996 asked for) rather than really sleeping.
"""

from __future__ import annotations

import asyncio
import subprocess

from backend.core.logging import get_logger
from backend.core.state import StateDB
from backend.core.supervisor import spawn_supervised

log = get_logger(__name__)

# Seconds to wait after a fix is applied before re-checking the container.
# A module constant so tests monkeypatch it to 0 instead of sleeping for real.
FIX_VERIFY_DELAY_S = 60


def _verify_fix_recovered(app_key: str, fix_history_id: int) -> None:
    """Re-check ``app_key``'s container and stamp the LINKED fix_history outcome.

    Synchronous (docker subprocess + DB write) — invoke via ``asyncio.to_thread``
    so it never blocks the event loop. Best-effort: any failure is swallowed, since
    a verification miss must never crash the supervised task.

    #822 Unit B: the row to stamp is addressed by ``fix_history_id`` (the explicit
    referential link recorded when the fix outcome was written) — NOT the old
    ``(app_key, MAX(created_at))`` heuristic, which stamped the WRONG row whenever
    more than one fix_history row existed for the app (e.g. a later unrelated
    failure landing inside the 60s verification window).
    """
    try:
        r = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Status}}", app_key],
            capture_output=True,
            text=True,
            timeout=5,
        )
        still_failing = r.returncode != 0 or r.stdout.strip() not in ("running",)
        log.info(
            "Post-fix verification for %s: %s",
            app_key,
            "recovered" if not still_failing else "still failing",
        )
        with StateDB() as db:
            db.execute(
                "UPDATE fix_history SET outcome=? WHERE id=?",
                (
                    "success" if not still_failing else "failed_verification",
                    fix_history_id,
                ),
            )
            # StateDB auto-commits on __exit__ (Core Rule 4.4).
    except Exception as e:  # best-effort — never crash the supervised task
        log.debug("post-fix verification skipped for %s: %s", app_key, e)


def schedule_fix_verification(app_key: str, fix_history_id: int) -> None:
    """Schedule a post-fix verification as a SUPERVISED background task (#996).

    Replaces the old ``threading.Thread(daemon=True)``. Fire-and-forget but
    supervised: ``spawn_supervised`` holds a reference (no GC), records the task's
    health, and dedups by name so re-approving the same app does not stack
    duplicate checks. Must be called from within the running event loop (it is —
    the caller, ``approve_fix``, is async).

    ``fix_history_id`` (#822 Unit B) is the row the verification will stamp —
    addressed by id, not by an (app_key, MAX(created_at)) guess.
    """

    async def _verify_after_delay() -> None:
        await asyncio.sleep(FIX_VERIFY_DELAY_S)
        await asyncio.to_thread(_verify_fix_recovered, app_key, fix_history_id)

    spawn_supervised(f"fix-verify:{app_key}", _verify_after_delay)
