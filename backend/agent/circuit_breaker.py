"""backend/agent/circuit_breaker.py

Global and per-app autofix circuit-breakers.

Global:
  Counts applied fixes in the last hour against a global cap. If the cap is
  reached the circuit opens (returns open=False) so the scheduler skips further
  auto-apply until the window rolls. Fail-closed: DB unreachable → open=False.

Per-app:
  Independently caps fixes per app per hour so one flapping app cannot exhaust
  the global budget for the whole fleet. Fail-closed: DB unreachable → open=False.

Failed-fix read-back:
  Queries fix_history for repeated failed_verification outcomes on (app, fix_type).
  When ≥ threshold failures occur within window_s, returns should_escalate=True so
  callers stop re-suggesting the same fix and emit an escalate action instead.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from backend.core.logging import get_logger

log = get_logger(__name__)


@dataclass(frozen=True)
class CircuitBreakerResult:
    """Immutable result returned by check_circuit()."""

    open: bool  # True = allow auto-apply; False = block
    fixes_last_hour: int
    cap: int
    reason: str  # "ok" | "cap_exceeded" | "db_unreachable"


@dataclass(frozen=True)
class AppCircuitBreakerResult:
    """Immutable result returned by check_app_circuit()."""

    open: bool  # True = allow auto-apply; False = block
    app_key: str
    fixes_last_hour: int
    cap: int
    reason: str  # "ok" | "cap_exceeded" | "db_unreachable"


@dataclass(frozen=True)
class FailedFixResult:
    """Immutable result returned by check_failed_fix_history()."""

    should_escalate: bool  # True = stop retrying, emit escalate action
    app_key: str
    fix_type: str
    failure_count: int
    threshold: int
    reason: str  # "ok" | "threshold_exceeded" | "db_unreachable"


def check_circuit(*, cap: int = 10, window_s: int = 3600) -> CircuitBreakerResult:
    """Check the global autofix rate against *cap* fixes per *window_s* seconds.

    Queries the count of applied fixes whose resolved_at is within the last
    window_s seconds. Logs only the count, never individual fix IDs or app keys.

    Returns:
        CircuitBreakerResult with open=True when auto-apply is permitted,
        open=False when the cap is exceeded or the DB is unreachable.
    """
    cutoff = int(time.time()) - window_s
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM pending_fixes"
                " WHERE status='applied' AND resolved_at >= ?",
                (cutoff,),
            ).fetchone()
        fixes_last_hour = row["n"] if row else 0
    except Exception as exc:
        log.warning("circuit_breaker: StateDB unreachable: %s", exc)
        return CircuitBreakerResult(
            open=False,
            fixes_last_hour=0,
            cap=cap,
            reason="db_unreachable",
        )

    log.debug(
        "circuit_breaker: %d/%d fixes in last %ds",
        fixes_last_hour,
        cap,
        window_s,
    )

    if fixes_last_hour >= cap:
        return CircuitBreakerResult(
            open=False,
            fixes_last_hour=fixes_last_hour,
            cap=cap,
            reason="cap_exceeded",
        )
    return CircuitBreakerResult(
        open=True,
        fixes_last_hour=fixes_last_hour,
        cap=cap,
        reason="ok",
    )


def check_app_circuit(
    app_key: str, *, cap: int = 5, window_s: int = 3600
) -> AppCircuitBreakerResult:
    """Check the per-app autofix rate against *cap* fixes per *window_s* seconds.

    Counts applied pending_fixes rows for *app_key* within the last window_s
    seconds. This runs ALONGSIDE the global check_circuit — one flapping app
    cannot exhaust the global budget for the whole fleet.

    Fail-closed: DB unreachable → open=False.

    Returns:
        AppCircuitBreakerResult with open=True when auto-apply is permitted for
        this app, open=False when its per-app cap is exceeded or DB is unreachable.
    """
    cutoff = int(time.time()) - window_s
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM pending_fixes"
                " WHERE app_key=? AND status='applied' AND resolved_at >= ?",
                (app_key, cutoff),
            ).fetchone()
        fixes_last_hour = row["n"] if row else 0
    except Exception as exc:
        log.warning("circuit_breaker: per-app StateDB unreachable for %s: %s", app_key, exc)
        return AppCircuitBreakerResult(
            open=False,
            app_key=app_key,
            fixes_last_hour=0,
            cap=cap,
            reason="db_unreachable",
        )

    log.debug(
        "circuit_breaker[%s]: %d/%d fixes in last %ds",
        app_key,
        fixes_last_hour,
        cap,
        window_s,
    )

    if fixes_last_hour >= cap:
        log.warning(
            "circuit_breaker: per-app cap exceeded for %s (%d/%d in last %ds)",
            app_key,
            fixes_last_hour,
            cap,
            window_s,
        )
        return AppCircuitBreakerResult(
            open=False,
            app_key=app_key,
            fixes_last_hour=fixes_last_hour,
            cap=cap,
            reason="cap_exceeded",
        )
    return AppCircuitBreakerResult(
        open=True,
        app_key=app_key,
        fixes_last_hour=fixes_last_hour,
        cap=cap,
        reason="ok",
    )


def check_failed_fix_history(
    app_key: str,
    fix_type: str,
    *,
    threshold: int = 3,
    window_s: int = 86400,
) -> FailedFixResult:
    """Check whether a (app_key, fix_type) pair has repeatedly failed verification.

    Queries fix_history for outcome='failed_verification' rows where:
      - app_key matches
      - context matches fix_type (context column stores the fix_type in apply.py)
      - created_at is within the last window_s seconds (default: 24h)

    When failure_count >= threshold, returns should_escalate=True. The caller
    must stop re-suggesting this fix and instead emit action_type='escalate'.

    Fail-closed: DB unreachable → should_escalate=False (allow retry; log warning).
    This is an intentional asymmetry: DB failure should not permanently suppress
    fixes — the safe failure mode here is to keep trying.

    Returns:
        FailedFixResult describing whether escalation is warranted.
    """
    cutoff = int(time.time()) - window_s
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            row = db.execute(
                "SELECT COUNT(*) AS n FROM fix_history"
                " WHERE app_key=? AND context=? AND outcome='failed_verification'"
                " AND created_at >= ?",
                (app_key, fix_type, cutoff),
            ).fetchone()
        failure_count = row["n"] if row else 0
    except Exception as exc:
        log.warning(
            "circuit_breaker: failed_fix_history DB unreachable for %s/%s: %s",
            app_key,
            fix_type,
            exc,
        )
        return FailedFixResult(
            should_escalate=False,
            app_key=app_key,
            fix_type=fix_type,
            failure_count=0,
            threshold=threshold,
            reason="db_unreachable",
        )

    log.debug(
        "circuit_breaker: %s/%s failed_verification count=%d (threshold=%d, window=%ds)",
        app_key,
        fix_type,
        failure_count,
        threshold,
        window_s,
    )

    if failure_count >= threshold:
        log.warning(
            "circuit_breaker: %s/%s hit failed_verification threshold (%d/%d in %ds)"
            " — escalating instead of retrying",
            app_key,
            fix_type,
            failure_count,
            threshold,
            window_s,
        )
        return FailedFixResult(
            should_escalate=True,
            app_key=app_key,
            fix_type=fix_type,
            failure_count=failure_count,
            threshold=threshold,
            reason="threshold_exceeded",
        )
    return FailedFixResult(
        should_escalate=False,
        app_key=app_key,
        fix_type=fix_type,
        failure_count=failure_count,
        threshold=threshold,
        reason="ok",
    )
