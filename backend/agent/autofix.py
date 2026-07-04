"""backend/agent/autofix.py

Selection layer for autonomous safe-tier auto-apply.

This module is the read-only contract the scheduler codes against. It answers
a single question: *which pending_fixes rows are eligible to be applied
autonomously, right now, without human approval?*

It deliberately does NOT mutate the DB or apply anything — applying is the job
of `backend.agent.apply.apply_safe_fix`, which the scheduler calls for each row
returned here and which enforces the backoff + post-fix health verification contract.

Eligibility (ALL must hold):
  - status == 'pending'
  - confidence >= min_confidence
  - get_fix_type(diagnosis_class) in SAFE_FIX_TYPES, MINUS the 'env_var_format'
    Phase-H stub (which is excluded from auto-apply by design).

`SAFE_FIX_TYPES` and `get_fix_type` are imported from `backend.agent.apply` —
the single source of truth. We do not hardcode a second copy of the taxonomy.

Confirmation gate (id=722, id=726):
  `apply_eligible_fixes` is the execution entry-point for the scheduler. It
  accepts a ``dry_run`` flag (default ``True``) that prevents any mutation when
  set. Callers MUST pass ``dry_run=False`` to execute fixes. This flag maps to
  ``OperationalLevel``: SUPERVISED uses dry_run=True (gate ON); AUTONOMOUS uses
  dry_run=False (gate bypassed), but only when explicitly set via settings.
"""

from __future__ import annotations

from typing import Any

from backend.core.logging import get_logger
from backend.core.state import StateDB
from backend.agent.apply import SAFE_FIX_TYPES, get_fix_type
from backend.agent.types import OperationalLevel

log = get_logger(__name__)

# The Phase-H stub is never auto-applied; subtract it from the safe set to get
# the set of fix_types eligible for autonomous apply.
_EXCLUDED_FIX_TYPES: frozenset[str] = frozenset({"env_var_format"})
AUTO_APPLICABLE_FIX_TYPES: frozenset[str] = frozenset(SAFE_FIX_TYPES) - _EXCLUDED_FIX_TYPES


def select_auto_applicable(*, min_confidence: float) -> list[Any]:
    """Return pending_fixes rows eligible for autonomous safe-tier apply:
    status='pending', confidence >= min_confidence, and
    get_fix_type(diagnosis_class) in SAFE_FIX_TYPES MINUS {'env_var_format'}.
    Ordered by confidence DESC. Read-only; never raises (returns [] on error)."""
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

    # Filter on fix_type in Python: get_fix_type maps diagnosis_class → fix_type
    # and is the single source of truth for the taxonomy. Keep only fix_types in
    # the auto-applicable set (safe tier minus the env_var_format stub).
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


def apply_eligible_fixes(
    rows: list[Any],
    *,
    dry_run: bool = True,
    operational_level: OperationalLevel = OperationalLevel.SUPERVISED,
) -> list[dict[str, Any]]:
    """Apply (or simulate) a list of pre-selected eligible fix rows.

    This is the **confirmation gate** for the auto-apply pipeline (id=722).

    Args:
        rows:              Rows returned by ``select_auto_applicable``.
        dry_run:           When ``True`` (default), log the proposed action and
                           return without mutating anything.  Callers MUST
                           explicitly pass ``dry_run=False`` to execute.
        operational_level: The agent's operational posture (id=726).
                           ADVISORY  → always dry-run regardless of the flag.
                           SUPERVISED → respects the ``dry_run`` flag (default
                             ``True`` means gate is ON).
                           AUTONOMOUS → bypasses the gate only when the caller
                             has read the level from settings and passed it
                             explicitly; ``dry_run`` is still honoured if True.

    Returns:
        List of result dicts per row: {"fix_id", "app_key", "dry_run", "ok", "message"}.
        Never raises.
    """
    from backend.agent.apply import apply_safe_fix

    # ADVISORY always skips execution regardless of dry_run flag.
    if operational_level is OperationalLevel.ADVISORY:
        log.info(
            "apply_eligible_fixes: ADVISORY level — skipping all %d fix(es) (no mutations)",
            len(rows),
        )
        return [
            {
                "fix_id": row["id"],
                "app_key": row["app_key"],
                "dry_run": True,
                "ok": None,
                "message": "skipped: ADVISORY operational level — no mutations permitted",
            }
            for row in rows
        ]

    # SUPERVISED with dry_run=True (the default) — log and skip.
    # AUTONOMOUS with dry_run=True — still honour the flag.
    effective_dry_run = dry_run

    results: list[dict[str, Any]] = []
    for row in rows:
        fix_id = row["id"]
        app_key = row["app_key"]

        if effective_dry_run:
            log.info(
                "apply_eligible_fixes: DRY-RUN fix_id=%s app_key=%s diagnosis=%s"
                " — pass dry_run=False to execute",
                fix_id,
                app_key,
                row["diagnosis_class"],
            )
            results.append(
                {
                    "fix_id": fix_id,
                    "app_key": app_key,
                    "dry_run": True,
                    "ok": None,
                    "message": (
                        f"dry_run=True: would apply fix_type="
                        f"{get_fix_type(row['diagnosis_class'])} — not executed"
                    ),
                }
            )
        else:
            try:
                log.info(
                    "AGENT-ACTION: trigger=auto_apply app=%s fix_type=%s fix_id=%s",
                    app_key,
                    row["diagnosis_class"],
                    fix_id,
                )
                result = apply_safe_fix(fix_id, row)
                results.append(
                    {
                        "fix_id": fix_id,
                        "app_key": app_key,
                        "dry_run": False,
                        **result,
                    }
                )
            except Exception as exc:
                log.warning(
                    "apply_eligible_fixes: apply_safe_fix raised fix_id=%s: %s",
                    fix_id,
                    exc,
                )
                results.append(
                    {
                        "fix_id": fix_id,
                        "app_key": app_key,
                        "dry_run": False,
                        "ok": False,
                        "message": f"exception: {exc}",
                    }
                )

    return results
