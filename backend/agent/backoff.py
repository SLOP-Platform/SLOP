"""backend/agent/backoff.py

Restart-oscillation guard for the SLOP Agent safe-apply tier.

Provides two pure, unit-testable functions that together implement an
exponential-backoff gate in front of auto-fix actions:

  attempt_allowed()  — read-only gate; returns (allowed, reason)
  record_attempt()   — append-only write; best-effort, never raises

Both functions use the project StateDB for reads/writes.  They must be
called from apply.py — NOT from API routers and NOT from the listener.

The backoff formula for the n-th attempt (n = attempts already recorded
in the window, 0-based counting after the first):

  must_wait_s = backoff_base_s * 2 ** (n - 1)

where n is the count of attempts in the window BEFORE the potential new
one.  When n == 0 (no prior attempts) no time gate is applied; only the
max_attempts cap matters.
"""

from __future__ import annotations

import time
from collections.abc import Callable

from backend.core.logging import get_logger
from backend.core.state import StateDB

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Monkeypatchable clock — tests override this to control time without sleeps
# ---------------------------------------------------------------------------

_now: Callable[[], float] = time.time


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def attempt_allowed(
    app_key: str,
    fix_type: str,
    *,
    max_attempts: int = 3,
    window_s: int = 3600,
    backoff_base_s: int = 60,
) -> tuple[bool, str]:
    """Return (allowed, reason).

    Deny if >= max_attempts within window_s, or if the last attempt was
    less than backoff_base_s * 2**(n-1) seconds ago (where n is the
    number of attempts already recorded in the window).
    """
    now = _now()
    cutoff = now - window_s

    try:
        with StateDB() as db:
            rows = db.execute(
                """
                SELECT created_at
                FROM   fix_attempts
                WHERE  app_key  = ?
                AND    fix_type = ?
                AND    created_at >= ?
                ORDER  BY created_at ASC
                """,
                (app_key, fix_type, cutoff),
            ).fetchall()
    except Exception as exc:
        # On DB error, fail open to avoid blocking legitimate fixes
        log.warning(
            "backoff.attempt_allowed: DB read failed for %s/%s: %s",
            app_key,
            fix_type,
            exc,
        )
        return True, "db-read-error (fail-open)"

    n = len(rows)

    # Cap check: deny when already at or over the limit
    if n >= max_attempts:
        return (
            False,
            f"backoff: {n} attempts for {app_key}/{fix_type} in the last "
            f"{window_s}s (max {max_attempts})",
        )

    # Time-spacing check: deny if we're inside the backoff interval
    if n > 0:
        last_ts = float(rows[-1]["created_at"])
        must_wait = backoff_base_s * (2 ** (n - 1))
        elapsed = now - last_ts
        if elapsed < must_wait:
            remaining = int(must_wait - elapsed)
            return (
                False,
                f"backoff: last attempt for {app_key}/{fix_type} was "
                f"{int(elapsed)}s ago; must wait {must_wait}s "
                f"({remaining}s remaining)",
            )

    return True, "allowed"


def record_attempt(app_key: str, fix_type: str, outcome: str) -> None:
    """Append a row to fix_attempts.

    Best-effort: any exception is logged and swallowed — this function
    must never raise so that a DB hiccup cannot abort a successful fix.
    """
    try:
        with StateDB() as db:
            db.execute(
                """
                INSERT INTO fix_attempts (app_key, fix_type, outcome)
                VALUES (?, ?, ?)
                """,
                (app_key, fix_type, outcome),
            )
    except Exception as exc:
        log.warning(
            "backoff.record_attempt: failed to record %s/%s (%s): %s",
            app_key,
            fix_type,
            outcome,
            exc,
        )
