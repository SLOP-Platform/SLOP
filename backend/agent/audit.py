"""backend/agent/audit.py — append-only agent action audit trail (N7 / #978).

Every autonomous (pre-approved) action executed by the SLOP agent is recorded
here BEFORE and AFTER execution.  This table is the authoritative "what did
you do" source for the chat panel's status queries (W6/N6).

Design constraints:
  * **Append-only**: rows are never updated or deleted.  A separate
    ``outcome`` UPDATE is performed by writing a new OUTCOME row keyed by the
    ``run_id`` from the initial QUEUED row — the initial row is never touched.
    This is enforced at the table level (no UPDATE privilege in the migration)
    and structurally (no UPDATE call exists in this module).
  * **Fail-closed notify**: every recorded action sends a best-effort ntfy
    notification through the existing escalation egress path (checker.py's
    ``_send_notification`` — the same channel as health failure alerts).  A
    notification failure does NOT prevent the action from proceeding — it is
    logged at WARNING.  This satisfies invariant 4 (no silent autonomy) without
    blocking the mutation path.
  * **No new egress path**: notification is routed through the existing ntfy
    channel used by ``_notify_failure`` / ``_notify_escalation``.  A cloud-
    bound escalation (e.g. for T3 or budget-exceeded events) routes through
    ``spine_egress.send_for_review`` unchanged.

Schema (migration 019_agent_action_audit.sql):
    agent_action_audit (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        run_id      TEXT NOT NULL,        -- stable per-invocation UUID
        ts          INTEGER NOT NULL,     -- unix epoch, set at INSERT time
        trigger     TEXT NOT NULL,        -- 'scheduler'|'chat'|'api'
        action_id   TEXT NOT NULL,        -- registry action id
        app_key     TEXT NOT NULL,        -- subject app
        tier        INTEGER NOT NULL,     -- ActionTier value (0-3)
        status      TEXT NOT NULL,        -- 'queued'|'ok'|'failed'|'rolled_back'
        outcome_msg TEXT,                 -- human-readable result / error
        rollback    INTEGER NOT NULL DEFAULT 0  -- 1 if rollback was triggered
    )
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

from backend.core.logging import get_logger
from backend.core.state import StateDB

log = get_logger(__name__)

# Table name constant — import from here rather than hardcoding in tests.
TABLE_AGENT_AUDIT = "agent_action_audit"

# Valid trigger sources — fail-closed: unknown trigger is replaced with
# 'unknown' rather than silently allowing arbitrary values.
_VALID_TRIGGERS = frozenset({"scheduler", "chat", "api", "unknown"})


def _coerce_trigger(trigger: str) -> str:
    return trigger if trigger in _VALID_TRIGGERS else "unknown"


# ---------------------------------------------------------------------------
# Write path — append-only
# ---------------------------------------------------------------------------


def record_action_queued(
    *,
    trigger: str,
    action_id: str,
    app_key: str,
    tier: int,
) -> str:
    """Append a QUEUED row and return the stable ``run_id`` for this invocation.

    Called BEFORE the handler fires so the audit trail captures intent even
    when the handler crashes.  Returns the run_id that callers pass to
    ``record_action_outcome`` after the handler completes.
    """
    run_id = str(uuid.uuid4())
    ts = int(time.time())
    try:
        with StateDB() as db:
            db.execute(
                f"""INSERT INTO {TABLE_AGENT_AUDIT}
                    (run_id, ts, trigger, action_id, app_key, tier, status)
                    VALUES (?, ?, ?, ?, ?, ?, 'queued')""",  # noqa: S608 - table is the fixed constant TABLE_AGENT_AUDIT; all values are ?-bound
                (run_id, ts, _coerce_trigger(trigger), action_id, app_key, int(tier)),
            )
            # StateDB commits on __exit__ — no explicit commit needed.
    except Exception as exc:
        # Audit failure must never block the action — log and continue.
        log.error(
            "audit: record_action_queued failed for %s/%s (run_id=%s): %s",
            action_id,
            app_key,
            run_id,
            exc,
        )
    return run_id


def record_action_outcome(
    run_id: str,
    *,
    status: str,
    outcome_msg: str = "",
    rollback: bool = False,
) -> None:
    """Append an OUTCOME row for *run_id* (append-only — the QUEUED row is never updated).

    *status* must be one of: ``'ok'``, ``'failed'``, ``'rolled_back'``.
    """
    valid_statuses = frozenset({"ok", "failed", "rolled_back"})
    if status not in valid_statuses:
        log.warning("audit: unknown status %r for run_id=%s — using 'failed'", status, run_id)
        status = "failed"
    ts = int(time.time())
    try:
        with StateDB() as db:
            db.execute(
                f"""INSERT INTO {TABLE_AGENT_AUDIT}
                    (run_id, ts, trigger, action_id, app_key, tier, status, outcome_msg, rollback)
                    SELECT run_id, ?, trigger, action_id, app_key, tier, ?, ?, ?
                    FROM {TABLE_AGENT_AUDIT}
                    WHERE run_id = ?
                    ORDER BY id LIMIT 1""",  # noqa: S608 - table is the fixed constant TABLE_AGENT_AUDIT; all values are ?-bound
                (ts, status, outcome_msg[:4096], 1 if rollback else 0, run_id),
            )
            # StateDB commits on __exit__ — no explicit commit needed.
    except Exception as exc:
        log.error("audit: record_action_outcome failed for run_id=%s: %s", run_id, exc)


# ---------------------------------------------------------------------------
# Notify-on-action — routed through existing ntfy egress (#983 path)
# ---------------------------------------------------------------------------


def notify_action(
    *,
    action_id: str,
    app_key: str,
    tier: int,
    status: str,
    outcome_msg: str = "",
    rollback: bool = False,
    ntfy_url: str = "http://ntfy:80",
    ntfy_topic: str = "slop",
) -> None:
    """Fire a best-effort ntfy notification for an autonomous agent action.

    Routes through the same ``_send_notification`` path as health failure
    alerts — no new egress path (W8 / invariant 4).  This is a synchronous
    wrapper: it schedules the coroutine on the running event loop if available,
    or runs it in a new loop as a fallback (test / CLI contexts).

    A send failure is logged at WARNING but never raises — the caller's
    mutation path is not affected.
    """
    emoji = "✅" if status == "ok" else ("↩️" if rollback else "❌")
    title = f"{emoji} Agent action: {action_id} on {app_key}"
    tier_label = {0: "T0-investigate", 1: "T1-reversible", 2: "T2-recoverable", 3: "T3-irreversible"}.get(
        tier, f"T{tier}"
    )
    parts = [
        f"App: {app_key}",
        f"Action: {action_id} ({tier_label})",
        f"Status: {status}",
    ]
    if outcome_msg:
        parts.append(f"Result: {outcome_msg[:200]}")
    if rollback:
        parts.append("Rollback: triggered")
    message = "\n".join(parts)

    async def _send() -> None:
        try:
            from backend.health.checker import _send_notification

            sent = await _send_notification(
                title=title,
                message=message,
                priority="default" if status == "ok" else "high",
                ntfy_url=ntfy_url,
                topic=ntfy_topic,
            )
            if not sent:
                log.warning(
                    "audit.notify_action: ntfy send returned False for %s/%s", action_id, app_key
                )
        except Exception as exc:
            log.warning("audit.notify_action: ntfy send failed for %s/%s: %s", action_id, app_key, exc)

    coro = _send()
    try:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop is not None and loop.is_running():
            # Inside a running loop: schedule and let it run concurrently.
            loop.create_task(coro)
        else:
            # No running loop (sync / CLI / test context): run to completion in a
            # fresh loop so the coroutine is always awaited (never orphaned).
            asyncio.run(coro)
    except Exception as exc:
        # Fail-open on NOTIFY only — the audit row write above is already durable
        # and never depends on notify success (W8 / invariant 4). Close the
        # coroutine if it never started, so it is not left un-awaited.
        coro.close()
        log.warning("audit.notify_action: event loop error for %s/%s: %s", action_id, app_key, exc)


# ---------------------------------------------------------------------------
# Read path — "what did you do" source for chat
# ---------------------------------------------------------------------------


def get_recent_actions(
    limit: int = 20,
    app_key: str | None = None,
) -> list[dict[str, Any]]:
    """Return the most recent audit rows, newest first.

    Used by the chat panel's status query and ``GET /api/health/agent-audit``.
    Returns only OUTCOME rows (status in ok/failed/rolled_back) so the caller
    sees final results, not pending queue entries.
    """
    try:
        with StateDB() as db:
            if app_key:
                rows = db.execute(
                    f"""SELECT run_id, ts, trigger, action_id, app_key, tier, status,
                               outcome_msg, rollback
                        FROM {TABLE_AGENT_AUDIT}
                        WHERE app_key = ? AND status IN ('ok', 'failed', 'rolled_back')
                        ORDER BY ts DESC, id DESC
                        LIMIT ?""",  # noqa: S608 - table is the fixed constant TABLE_AGENT_AUDIT; all values are ?-bound
                    (app_key, min(limit, 100)),
                ).fetchall()
            else:
                rows = db.execute(
                    f"""SELECT run_id, ts, trigger, action_id, app_key, tier, status,
                               outcome_msg, rollback
                        FROM {TABLE_AGENT_AUDIT}
                        WHERE status IN ('ok', 'failed', 'rolled_back')
                        ORDER BY ts DESC, id DESC
                        LIMIT ?""",  # noqa: S608 - table is the fixed constant TABLE_AGENT_AUDIT; all values are ?-bound
                    (min(limit, 100),),
                ).fetchall()
        return [dict(r) for r in rows]
    except Exception as exc:
        log.error("audit.get_recent_actions: read failed: %s", exc)
        return []


__all__ = [
    "TABLE_AGENT_AUDIT",
    "get_recent_actions",
    "notify_action",
    "record_action_outcome",
    "record_action_queued",
]
