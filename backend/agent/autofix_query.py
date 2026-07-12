"""backend/agent/autofix_query.py

Read-only pending_fixes selection contract — answers "which pending_fixes rows
are eligible for autonomous safe-tier apply, right now?"

This module does NOT mutate the DB or apply anything. It is the read-only
half of the autofix subsystem.  Separated from autofix_execute.py so tickets
that touch the execution path (#488 levels, #492 simulate, #490 correlation-id)
do NOT collide with features that change the selection query (#491 factual-tuples).
"""

from __future__ import annotations

from typing import Any

from backend.agent.apply import SAFE_FIX_TYPES, get_fix_type
from backend.core.logging import get_logger
from backend.core.state import StateDB

log = get_logger(__name__)

# The Phase-H stub is never auto-applied; subtract it from the safe set to get
# the set of fix_types eligible for autonomous apply.
_EXCLUDED_FIX_TYPES: frozenset[str] = frozenset({"env_var_format"})
AUTO_APPLICABLE_FIX_TYPES: frozenset[str] = frozenset(SAFE_FIX_TYPES) - _EXCLUDED_FIX_TYPES


def select_auto_applicable(*, min_confidence: float) -> list[Any]:
    """Return pending_fixes rows eligible for autonomous safe-tier apply.

    Filters by status='pending', confidence >= min_confidence, and
    get_fix_type(diagnosis_class) in SAFE_FIX_TYPES MINUS {'env_var_format'}.
    Ordered by confidence DESC. Read-only; never raises (returns [] on error).
    """
    try:
        with StateDB() as db:
            rows = db.execute(
                """
                SELECT *
                FROM   pending_fixes
                WHERE  status = 'pending'
                  AND  confidence >= ?
                ORDER BY confidence DESC
                """,
                (min_confidence,),
            ).fetchall()
    except Exception as exc:  # best-effort, never raise into caller
        log.warning("select_auto_applicable: read failed, returning []: %s", exc)
        return []

    eligible = [
        row for row in rows if get_fix_type(row["diagnosis_class"]) in AUTO_APPLICABLE_FIX_TYPES
    ]
    log.info(
        "select_auto_applicable: %d/%d pending rows eligible (min_confidence=%.2f)",
        len(eligible),
        len(rows),
        min_confidence,
    )
    return eligible
