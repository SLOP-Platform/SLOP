"""backend/agent/listener.py

Install-failure listener — Phase A/B/C of the LLM agent pipeline.

Architecture overview (§3 of LLM-AGENT-DESIGN.md):
  executor.py fires install_failure_listener() as a fire-and-forget
  coroutine whenever a step lands with status='error'.  This module
  writes a pending_fixes row with a real diagnosis_class determined by
  classify_with_llm() (Phase C), which falls back to the offline regex
  classifier if the LLM is unreachable.

Phase C change: replaced classify_offline() with classify_with_llm().
  The listener now persists LLM-sourced suggested_fix + confidence.
  Graceful degrade: if classify_with_llm() raises, falls back to
  classify_offline() + empty suggested_fix + confidence=0.0.

Phase F extension: install_failure_listener() is now also called by
  backend/agent/watcher.py (Docker event watcher) for runtime container
  failures (die, oom, health_status=unhealthy).  The step_log format is
  identical so the classifier + DB write path is reused unchanged.

Invariant: this module MUST be a no-op if anything goes wrong — it
must never propagate exceptions back into the install pipeline.
"""

from __future__ import annotations

import uuid
from typing import Any

from backend.agent.classifier import classify_offline, classify_with_llm
from backend.core.logging import get_correlation_id, get_logger, reset_correlation_id, set_correlation_id
from backend.health.swallow_counter import record_swallow

log = get_logger(__name__)

_CHECK_NAME = "install_monitor"
_ACTION_TYPE = "diagnose"
_PROBLEM_TRUNCATE = 512


async def install_failure_listener(app_key: str, step_log: dict[str, Any]) -> None:
    """Fire-and-forget coroutine called on every install step with status='error'.

    Only acts when step_log["status"] == "error".  All exceptions are
    swallowed — the install pipeline must never be slowed or broken by
    agent code.

    Phase C: calls classify_with_llm() for LLM-enriched diagnosis.
    Falls back to classify_offline() if anything goes wrong.

    Args:
        app_key:  The catalog key of the app being installed.
        step_log: A dict representation of a StepLog (name, status,
                  message, detail).
    """
    if step_log.get("status") != "error":
        return

    # Generate correlation_id if none is set (e.g. called from executor.py
    # without the watcher's context). The watcher entry point already sets
    # one with 'wa-' prefix; the listener uses 'ls-' so the source is obvious.
    _own_token = None
    _existing = get_correlation_id()
    if _existing == "(no-correlation)":
        _own_token = set_correlation_id(f"ls-{uuid.uuid4().hex[:12]}")

    try:
        problem = str(step_log.get("detail") or step_log.get("message") or "")[:_PROBLEM_TRUNCATE]

        # Phase C: classify with LLM (3-step fallback).
        # db_path is obtained from the global StateDB configuration.
        error_class_val = "UNKNOWN"
        suggested_fix = ""
        confidence = 0.0

        try:
            import backend.core.state as _state_mod

            db_path_str = str(_state_mod._DB_PATH) if _state_mod._DB_PATH is not None else ""
            error_class, suggested_fix, confidence = await classify_with_llm(
                problem, app_key, db_path_str
            )
            error_class_val = error_class.value
        except Exception as exc:
            # classify_with_llm failed entirely (should never happen — but be safe).
            log.debug(
                "install_failure_listener: classify_with_llm failed for %s: %s",
                app_key,
                exc,
            )
            record_swallow("listener.classify_with_llm_fallback")
            error_class_val = classify_offline(problem).value
            suggested_fix = ""
            confidence = 0.0

        try:
            from backend.core.state import StateDB

            with StateDB() as db:
                _write_pending_fix(db, app_key, problem, error_class_val, suggested_fix, confidence)
        except Exception as exc:
            # Never propagate — agent is a best-effort observer.
            log.debug("install_failure_listener: DB write failed for %s: %s", app_key, exc)
            record_swallow("listener.pending_fix_db_write")
    finally:
        if _own_token is not None:
            reset_correlation_id(_own_token)


def _write_pending_fix(
    db: Any,
    app_key: str,
    problem: str,
    diagnosis_class: str = "UNKNOWN",
    suggested_fix: str = "",
    confidence: float = 0.0,
) -> None:
    """Insert or upsert a pending_fixes row for this install failure.

    Uses ON CONFLICT to update an existing row so repeated failures on
    the same app don't stack up duplicate rows.

    Phase C: *suggested_fix* and *confidence* now carry real LLM output.
    When the LLM is unreachable they default to ("", 0.0).

    Args:
        db:               An open StateDB context-manager instance.
        app_key:          The catalog key of the failing app.
        problem:          Truncated error detail string (max 512 chars).
        diagnosis_class:  String value of the matched ErrorClass enum.
        suggested_fix:    Short LLM-generated fix suggestion (may be "").
        confidence:       Float confidence score (0.0-0.95).
    """
    db.execute(
        """
        INSERT INTO pending_fixes
            (app_key, check_name, action_type, problem, suggested_fix,
             confidence, status, diagnosis_class)
        VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
        ON CONFLICT(app_key, check_name, action_type)
        DO UPDATE SET
            problem        = excluded.problem,
            suggested_fix  = excluded.suggested_fix,
            confidence     = excluded.confidence,
            status         = 'pending',
            diagnosis_class= excluded.diagnosis_class,
            created_at     = unixepoch(),
            resolved_at    = NULL,
            fix_history_id = NULL
        """,
        (app_key, _CHECK_NAME, _ACTION_TYPE, problem, suggested_fix, confidence, diagnosis_class),
    )
