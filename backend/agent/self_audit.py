"""backend/agent/self_audit.py — the GROUND self-reconciler.

Reference implementation of the spine's ``reconcile()`` seam: reconcile the SLOP
Agent against **physics ONLY** and emit a frozen-verdict :class:`Finding` per
ground source.  No doc/process/runbook reads (two-owner firewall — survey §4).

Three ground sources, each its own Finding:

  1. **DB record vs runtime** — the agent's ``slop_agent`` app row (tier-0,
     category="agent") must exist and report a runtime-consistent status.  A
     missing row, or a row whose tier/category drifted from the canonical
     constants, is a GROUND DRIFT.  An unreachable DB is INDETERMINATE (loud).
  2. **RealityView** — ``get_reality_view()`` (bound_port / install_dir_is_git /
     install_dir_owner) must be internally coherent: a zero ``bound_port`` or an
     ``"unknown"`` owner is the view's own never-raises fallback firing, i.e. the
     ground source was unreachable -> INDETERMINATE, not a silent OK.
  3. **Enforcement-coverage integrity** — ``run_process_integrity_check()``
     reconciles SLOP's own rule-enforcement coverage.  A critical gap is a
     GROUND DRIFT; a non-OK probe that could not run is INDETERMINATE.

INDETERMINATE (loud) whenever a ground source is unreachable; never a silent
VERIFIED.  This module reads no docs.
"""

from __future__ import annotations

from backend.agent.spine import Finding, Verdict
from backend.core.logging import get_logger

log = get_logger(__name__)


def _reconcile_db_record() -> Finding:
    """GROUND source 1: the agent's DB record vs the canonical runtime identity."""
    physics = "StateDB apps row key=slop_agent (tier-0, category=agent)"
    try:
        from backend.core.agent import (
            AGENT_CATEGORY,
            AGENT_KEY,
            AGENT_TIER,
        )
        from backend.core.state import StateDB

        with StateDB() as db:
            row = db.get_app(AGENT_KEY)
    except Exception as exc:  # unreachable ground source is loud
        log.warning("self_audit db-record source unreachable: %s", exc)
        return Finding(
            id="self_audit.db_record",
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="agent DB record unreachable",
            detail=f"StateDB read raised: {type(exc).__name__}",
        )

    if row is None:
        return Finding(
            id="self_audit.db_record",
            physics=physics,
            verdict=Verdict.DRIFT,
            summary="agent DB record missing",
            detail="no apps row for key=slop_agent; ensure_agent_registered did not run",
        )
    if row.tier != AGENT_TIER or row.category != AGENT_CATEGORY:
        return Finding(
            id="self_audit.db_record",
            physics=physics,
            verdict=Verdict.DRIFT,
            summary="agent DB record identity drifted",
            detail=(
                f"expected tier={AGENT_TIER} category={AGENT_CATEGORY}; "
                f"got tier={row.tier} category={row.category}"
            ),
        )
    return Finding(
        id="self_audit.db_record",
        physics=physics,
        verdict=Verdict.VERIFIED,
        summary="agent DB record present and identity-consistent",
        detail=f"tier={row.tier} category={row.category} status={row.status}",
    )


def _reconcile_reality_view() -> Finding:
    """GROUND source 2: the RealityView is internally coherent (not a fallback)."""
    physics = "get_reality_view() bound_port / install_dir_is_git / install_dir_owner"
    try:
        from backend.core.agent import get_reality_view

        view = get_reality_view()
    except Exception as exc:
        log.warning("self_audit reality-view source unreachable: %s", exc)
        return Finding(
            id="self_audit.reality_view",
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="RealityView unreachable",
            detail=f"get_reality_view raised: {type(exc).__name__}",
        )

    bound_port = view.get("bound_port", 0)
    owner = view.get("install_dir_owner", "unknown")
    # The view's never-raises fallback sets bound_port=0 + owner="unknown".  If we
    # see that shape, the ground source did not actually observe physics -> loud.
    if not bound_port or owner == "unknown":
        return Finding(
            id="self_audit.reality_view",
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="RealityView returned its unreachable fallback",
            detail=f"bound_port={bound_port} install_dir_owner={owner!r}",
        )
    return Finding(
        id="self_audit.reality_view",
        physics=physics,
        verdict=Verdict.VERIFIED,
        summary="RealityView observed live physics",
        detail=(
            f"bound_port={bound_port} "
            f"install_dir_is_git={view.get('install_dir_is_git')} owner={owner!r}"
        ),
    )


def _reconcile_integrity() -> Finding:
    """GROUND source 3: enforcement-coverage integrity (no doc reads — it shells
    out to ms-coverage, a physics probe of the rule graph)."""
    physics = "run_process_integrity_check() — ms-coverage rule-graph gaps"
    try:
        from backend.agent.integrity import run_process_integrity_check

        result = run_process_integrity_check()
    except Exception as exc:
        log.warning("self_audit integrity source unreachable: %s", exc)
        return Finding(
            id="self_audit.integrity",
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="enforcement-coverage probe unreachable",
            detail=f"run_process_integrity_check raised: {type(exc).__name__}",
        )

    if result.critical_gaps > 0:
        return Finding(
            id="self_audit.integrity",
            physics=physics,
            verdict=Verdict.DRIFT,
            summary="critical enforcement-coverage gap(s)",
            detail=result.summary,
        )
    if not result.ok and result.total_rules == 0:
        # ok=False with zero rules means the probe could not actually evaluate.
        return Finding(
            id="self_audit.integrity",
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="enforcement-coverage probe produced no rules",
            detail=result.summary,
        )
    return Finding(
        id="self_audit.integrity",
        physics=physics,
        verdict=Verdict.VERIFIED,
        summary="no critical enforcement-coverage gaps",
        detail=result.summary,
    )


def reconcile() -> list[Finding]:
    """The GROUND self-reconciler: reconcile the agent against physics only.

    Returns one :class:`Finding` per ground source.  Each leg is independently
    guarded so one unreachable source yields its own INDETERMINATE without
    suppressing the others.  Reads no docs (firewall).
    """
    return [
        _reconcile_db_record(),
        _reconcile_reality_view(),
        _reconcile_integrity(),
    ]


__all__ = ["reconcile"]
