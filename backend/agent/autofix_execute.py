"""backend/agent/autofix_execute.py

Execution / confirmation gate for the auto-apply pipeline (id=722, id=726).

Import ``select_auto_applicable`` from ``autofix_query`` (the read-only half)
and the execution helper ``apply_safe_fix`` from ``apply`` (lazy import to keep
the executor out of module scope until a real execution call).

Separated from ``autofix_query.py`` so tickets touching OperationalLevel branches
(#488), simulation (#492), or correlation-id threading (#490) collide only with
THIS file, not with the selection query.

5-stage pipeline (id=492):
  1. Observe  — Findings classified by the spine (elsewhere).
  2. Explain  — Root-cause explanation (spine_remediate).
  3. Recommend — Ranked recommendations (spine_remediate).
  4. Simulate — ``simulate()``: structured output of what each fix WOULD do.
  5. Fix      — ``apply_safe_fix()``: execute (or not, per operational level).
"""

from __future__ import annotations

import uuid
from typing import Any

from backend.agent.autofix_query import (  # noqa: F401
    AUTO_APPLICABLE_FIX_TYPES,
    select_auto_applicable,
)
from backend.agent.types import OperationalLevel
from backend.core.logging import get_correlation_id, get_logger, reset_correlation_id, set_correlation_id

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Stage 4: Simulate — structured output of what each fix WOULD do (id=492)
# ---------------------------------------------------------------------------

_FIX_TYPE_SIMULATION: dict[str, dict[str, str]] = {
    "restart_container": {
        "action": "restart_container",
        "command_description": "docker restart <container>",
        "expected_outcome": (
            "Container restarted with current image; health check passes "
            "within standard timeout"
        ),
        "risk_level": "low",
    },
    "repull_restart": {
        "action": "repull_restart",
        "command_description": (
            "docker pull <image> && docker stop <container> && "
            "docker start <container>"
        ),
        "expected_outcome": (
            "Container running latest image tag; health check passes "
            "within extended timeout"
        ),
        "risk_level": "medium",
    },
    "env_var_format": {
        "action": "env_var_format",
        "command_description": "update environment variable configuration (Phase-H stub — never auto-applied)",
        "expected_outcome": "Environment variable format corrected in container definition",
        "risk_level": "high",
    },
}


def simulate(
    row: dict[str, Any],
    *,
    operational_level: OperationalLevel = OperationalLevel.SUPERVISED,
) -> dict[str, Any]:
    """Stage 4: **Simulate** — produce structured output of what a fix WOULD do.

    Returns a dict describing the simulation with keys:
      * ``fix_id``, ``app_key``, ``fix_type`` — identifying the target fix.
      * ``simulated`` — nested dict with ``action``, ``target_container``,
        ``command_description``, ``expected_outcome``, ``risk_level``.
      * ``would_execute`` — ``True`` only when the operational level permits
        actual execution (SUPERVISED with dry_run=False, or AUTONOMOUS).

    This produces structured, auditable data — it is NOT a boolean gate.
    Unknown fix types receive a conservative simulation that assumes
    manual-only intervention.
    """
    from backend.agent.apply import get_fix_type as _get_fix_type

    fix_type = _get_fix_type(row.get("diagnosis_class", ""))
    sim_template = _FIX_TYPE_SIMULATION.get(
        fix_type,
        {
            "action": fix_type or "unknown",
            "command_description": "no automated fix mapped — manual intervention required",
            "expected_outcome": "dependent on manual investigation",
            "risk_level": "high",
        },
    )

    execution_allowed = operational_level in {
        OperationalLevel.SUPERVISED,
        OperationalLevel.AUTONOMOUS,
    }

    return {
        "fix_id": row.get("id"),
        "app_key": row.get("app_key"),
        "fix_type": fix_type,
        "simulated": {
            "action": sim_template["action"],
            "target_container": row.get("app_key", "unknown"),
            "command_description": sim_template["command_description"],
            "expected_outcome": sim_template["expected_outcome"],
            "risk_level": sim_template["risk_level"],
        },
        "would_execute": execution_allowed,
    }


def apply_eligible_fixes(
    rows: list[Any],
    *,
    dry_run: bool = True,
    operational_level: OperationalLevel = OperationalLevel.SUPERVISED,
) -> list[dict[str, Any]]:
    """Apply (or simulate) a list of pre-selected eligible fix rows.

    This is the **confirmation gate** for the auto-apply pipeline (id=722).

    5-stage pipeline (id=492):
      1. Observe   — elsewhere (spine classifies findings).
      2. Explain   — elsewhere (spine_remediate.explain).
      3. Recommend — elsewhere (spine_remediate.recommend).
      4. Simulate  — this module: ``simulate()`` produces structured output.
      5. Fix       — this module: ``apply_safe_fix()`` executes.

    Args:
        rows:              Rows returned by ``select_auto_applicable``.
        dry_run:           When ``True`` (default), log the proposed action and
                           return without mutating anything.  Callers MUST
                           explicitly pass ``dry_run=False`` to execute.
        operational_level: The agent's operational posture (id=726).
                           OBSERVE    → skip all, no mutations.
                           ADVISORY   → skip all, no mutations.
                           RECOMMEND  → **simulate** every fix: produce structured
                             simulation output without executing (id=492).
                           SUPERVISED → respects the ``dry_run`` flag (default
                             ``True`` means gate is ON); simulation is included.
                           AUTONOMOUS → bypasses the gate only when the caller
                             has read the level from settings and passed it
                             explicitly; ``dry_run`` is still honoured if True;
                             simulation is included.

    Returns:
        List of result dicts per row: {"fix_id", "app_key", "dry_run", "ok",
        "message", "simulation"}.  At RECOMMEND level, ``simulation`` is always
        present and ``ok`` is None (no execution).  At SUPERVISED/AUTONOMOUS,
        ``simulation`` is included alongside execution results for audit.
        Never raises.
    """
    # Generate correlation_id if none is set — the scheduler path has no
    # upstream correlation_id; the watcher→listener path already has one.
    _own_token = None
    _existing = get_correlation_id()
    if _existing == "(no-correlation)":
        _own_token = set_correlation_id(f"fx-{uuid.uuid4().hex[:12]}")

    try:
        _results = _apply(rows, dry_run, operational_level)
    finally:
        if _own_token is not None:
            reset_correlation_id(_own_token)

    return _results


def _apply(
    rows: list[Any],
    dry_run: bool,
    operational_level: OperationalLevel,
) -> list[dict[str, Any]]:
    """Internal: apply (or simulate) eligible fixes after correlation_id is set.

    Pipeline (id=492):
      4. **Simulate** — ``simulate()``: structured output before any action.
      5. **Fix**      — ``apply_safe_fix()``: only when level permits execution.
    """

    from backend.agent.apply import apply_safe_fix, get_fix_type as _get_fix_type_gf

    # SAFETY: Only explicit non-advisory levels may proceed past the gate.  Any
    # unrecognised value (including future levels added without updating this
    # gate) is treated as ADVISORY — fail-closed, not fail-open (#488).
    _execution_allowed = operational_level in {
        OperationalLevel.SUPERVISED,
        OperationalLevel.AUTONOMOUS,
    }

    # -- RECOMMEND level: simulate every row, never execute ---------------
    if operational_level == OperationalLevel.RECOMMEND:
        log.info(
            "apply_eligible_fixes: operational_level=%s — simulating %d fix(es) "
            "(no mutations)",
            operational_level.value,
            len(rows),
            correlation_id=get_correlation_id(),
        )
        results: list[dict[str, Any]] = []
        for row in rows:
            sim = simulate(row, operational_level=operational_level)
            results.append(
                {
                    "fix_id": row["id"],
                    "app_key": row["app_key"],
                    "dry_run": True,
                    "ok": None,
                    "message": (
                        f"SIMULATE: would apply {sim['simulated']['action']} on "
                        f"container '{sim['simulated']['target_container']}' — not "
                        f"executed (RECOMMEND level)"
                    ),
                    "simulation": sim,
                },
            )
        return results

    # -- OBSERVE / ADVISORY: skip all, no simulation -----------------------
    if not _execution_allowed:
        log.info(
            "apply_eligible_fixes: operational_level=%s — skipping all %d fix(es) (no mutations)",
            operational_level.value if hasattr(operational_level, "value") else operational_level,
            len(rows),
            correlation_id=get_correlation_id(),
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

    # -- SUPERVISED / AUTONOMOUS: simulate, then optionally execute ---------
    effective_dry_run = dry_run

    results = []
    for row in rows:
        fix_id = row["id"]
        app_key = row["app_key"]

        # Stage 4: Simulate — always produce structured simulation for audit
        sim = simulate(row, operational_level=operational_level)

        if effective_dry_run:
            log.info(
                "apply_eligible_fixes: DRY-RUN fix_id=%s app_key=%s diagnosis=%s"
                " — pass dry_run=False to execute",
                fix_id,
                app_key,
                row["diagnosis_class"],
                correlation_id=get_correlation_id(),
            )
            results.append(
                {
                    "fix_id": fix_id,
                    "app_key": app_key,
                    "dry_run": True,
                    "ok": None,
                    "message": (
                        f"dry_run=True: would apply fix_type="
                        f"{_get_fix_type_gf(row['diagnosis_class'])} — not executed"
                    ),
                    "simulation": sim,
                },
            )
        else:
            try:
                log.info(
                    "AGENT-ACTION: trigger=auto_apply app=%s fix_type=%s fix_id=%s",
                    app_key,
                    row["diagnosis_class"],
                    fix_id,
                    correlation_id=get_correlation_id(),
                )
                apply_result = apply_safe_fix(fix_id, row, operational_level=operational_level)
                results.append(
                    {
                        "fix_id": fix_id,
                        "app_key": app_key,
                        "dry_run": False,
                        **apply_result,
                        "simulation": sim,
                    },
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
                        "simulation": sim,
                    },
                )

    return results
