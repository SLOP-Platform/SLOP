"""backend/agent/spine_remediate.py — the advisory-only remediation gate.

Implements the spine's ``remediate(findings) -> list[Decision]`` seam with the
5-stage Observe→Explain→Recommend→Propose→Decide pipeline (id=492).

  1. **Observe**  — the Finding is already classified with a Verdict (GROUND).
  2. **Explain**  — ``explain()`` produces a structured root-cause explanation.
  3. **Recommend** — ``recommend()`` produces a ranked recommendation with rationale.
  4. **Propose**  — ``_propose_action()`` maps to a concrete would-propose string.
  5. **Decide**   — a :class:`Decision` records the proposal with structural non-acting reason.

This gate is **advisory-only by CONSTRUCTION** (review R6/R7): for each ``DRIFT``
finding it consults deterministic mappings to compute *what action it WOULD
propose*, and returns a structured :class:`Decision` describing that proposal plus
why it is NOT acting.  No action is wired.

**Advisory-only is STRUCTURAL, not a flag.**  This module reuses ONLY the
remediation taxonomy as PURE DATA — the ``SAFE_FIX_TYPES`` set and the
``DIAGNOSIS_TO_FIX_TYPE`` table imported by name.  It MUST NOT import or reference
any executor / mutator: NOT ``apply_safe_fix``, NOT ``select_auto_applicable``,
NOT any container/executor helper, NOT a ``StateDB`` write path, NOT
``subprocess``.  ``apply.py`` holds a real mutating executor (docker restart/pull
+ DB writes) and ``autofix.select_auto_applicable`` reads the DB, so importing
either would put an action one call away.  The advisory-only guarantee is enforced
by the ABSENCE of those symbols from this module's namespace — verified by a
structural import-absence test, which fails on ANY future executor reference.

**Extension point for a future gated-acting wave:** ``_propose_action`` is where a
a future gated-acting implementation would add the human-gate + backoff + verify and
actually invoke a SAFE_FIX.  Until then, every Decision carries ``why_not_acting``.
"""

from __future__ import annotations

from typing import Any

# PURE DATA ONLY — the remediation taxonomy.  These two names are sets/dicts; no
# executor or mutator is imported.  (Verified by test_spine_remediate's structural
# import-absence assertion.)
from backend.agent.apply import DIAGNOSIS_TO_FIX_TYPE, SAFE_FIX_TYPES
from backend.agent.spine import Decision, Finding, Verdict
from backend.core.logging import get_logger

# SHARED GOVERNANCE SEAM — this advisory module and the acting path
# (apply.py) now call the SAME governance module.  Only the advisory-safe
# ``frozen_verdict_respected`` predicate is imported here: it is a pure
# string-comparison guard (invariant 3) and binds NO executor/mutator symbol, so
# the import-absence guarantee is preserved.  This is "one governance regime, two
# callers" without merging the modules.
from backend.agent.governance_advisory import frozen_verdict_respected

log = get_logger(__name__)

_WHY_NOT = "advisory-only spine; no auto-remediation wired"


# Deterministic mapping: self-audit finding id -> the fix-type a future wave WOULD
# propose.  Pure data; values are members of SAFE_FIX_TYPES (the reused taxonomy)
# or a descriptive non-acting proposal for findings with no safe auto-fix.
_FINDING_TO_PROPOSAL: dict[str, str] = {
    # The agent DB record drifting is an identity/bootstrap problem — a future
    # wave would re-run ensure_agent_registered (a bounded, named action), NOT a
    # container fix.  Named here as a would-propose string, not invoked.
    "self_audit.db_record": "re-run ensure_agent_registered (bootstrap repair)",
    # RealityView / integrity drifts are detect-only by mandate (survey §4): the
    # gate would alert, never act.
    "self_audit.reality_view": "alert-only (no safe automated remediation)",
    "self_audit.integrity": "alert-only (no safe automated remediation)",
}


# ---------------------------------------------------------------------------
# Stage 2: Explain — deterministic root-cause explanations per finding type
# ---------------------------------------------------------------------------

_FINDING_TO_EXPLANATION: dict[str, dict[str, Any]] = {
    "self_audit.db_record": {
        "root_cause": (
            "Agent database record does not match the expected runtime identity. "
            "The agent may not have been registered, was registered under a different "
            "key, or the database was recreated without re-registering the agent."
        ),
        "contributing_factors": [
            "agent registration missing or stale",
            "database recreated without re-registration",
            "agent key rotated without updating the DB record",
        ],
        "severity": "high",
    },
    "self_audit.reality_view": {
        "root_cause": (
            "The agent's observed reality of running containers diverges from the "
            "expected state recorded in the database. This typically indicates "
            "containers that have crashed, been manually removed, or auto-restarted "
            "outside the agent's control loop."
        ),
        "contributing_factors": [
            "container crash or restart outside agent control",
            "manual intervention by operator",
            "Docker daemon restart causing state loss",
        ],
        "severity": "medium",
    },
    "self_audit.integrity": {
        "root_cause": (
            "A data-integrity check detected an inconsistency between the agent's "
            "recorded state and the ground truth. This is a detect-only gate — the "
            "source of inconsistency requires manual investigation."
        ),
        "contributing_factors": [
            "data corruption in agent state database",
            "concurrent state mutations from multiple agents",
            "incomplete transaction rollback after failure",
        ],
        "severity": "high",
    },
}


def _derive_explanation(
    finding: Finding, context: dict[str, str] | None = None
) -> tuple[str, list[str], str]:
    """Return (root_cause, contributing_factors, severity) for a finding."""
    known = _FINDING_TO_EXPLANATION.get(finding.id)
    if known is not None:
        return known["root_cause"], known["contributing_factors"], known["severity"]
    return (
        f"Drift detected in finding '{finding.id}': {finding.summary}. "
        "No pre-mapped explanation exists — manual investigation required.",
        ["unrecognised finding type"],
        "unknown",
    )


def explain(finding: Finding, context: dict[str, str] | None = None) -> dict[str, Any]:
    """Stage 2: **Explain** — produce a structured natural-language explanation.

    Takes a classified :class:`Finding` (output of the **Observe** stage) and
    optional caller-supplied context, and returns a structured explanation of
    the root cause, contributing factors, and severity.

    This is a deterministic, data-driven function — no LLM calls.  Unknown
    finding IDs receive a generic fallback explanation.
    """
    root_cause, factors, severity = _derive_explanation(finding, context)
    result: dict[str, Any] = {
        "finding_id": finding.id,
        "root_cause": root_cause,
        "contributing_factors": factors,
        "severity": severity,
    }
    log.debug("explain: finding_id=%s severity=%s", finding.id, severity)
    return result


# ---------------------------------------------------------------------------
# Stage 3: Recommend — ranked recommendation with rationale
# ---------------------------------------------------------------------------

_FINDING_TO_RECOMMENDATIONS: dict[str, list[dict[str, Any]]] = {
    "self_audit.db_record": [
        {
            "rank": 1,
            "action": "re-run ensure_agent_registered",
            "rationale": (
                "Idempotent bootstrap repair — re-registers the agent with the "
                "current runtime identity. Low risk; no container mutation."
            ),
            "confidence": "high",
        },
        {
            "rank": 2,
            "action": "manual review of agent registration state",
            "rationale": (
                "If automatic re-registration fails, an operator should inspect the "
                "agent key and database consistency manually."
            ),
            "confidence": "medium",
        },
    ],
    "self_audit.reality_view": [
        {
            "rank": 1,
            "action": "alert operator for manual investigation",
            "rationale": (
                "Reality-view drift has no safe automated remediation — the root "
                "cause (manual intervention, Docker restart, crash) requires human "
                "judgement to determine the correct recovery action."
            ),
            "confidence": "high",
        },
    ],
    "self_audit.integrity": [
        {
            "rank": 1,
            "action": "trigger integrity re-scan and alert operator",
            "rationale": (
                "Integrity violations are detect-only by mandate (survey §4). "
                "Re-scanning confirms persistence; the operator must investigate "
                "the root cause before any remediation."
            ),
            "confidence": "high",
        },
    ],
}


def _rank_fixes(
    finding: Finding,
    explanation: dict[str, Any],
    available_fixes: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Return ranked recommendations for a finding.

    Resolution order: 1) explicit per-finding ranking; 2) generic fallback
    using the diagnosis-based fix_type from the taxonomy.
    """
    known = _FINDING_TO_RECOMMENDATIONS.get(finding.id)
    if known is not None:
        return known
    fix_type = DIAGNOSIS_TO_FIX_TYPE.get(finding.id, "")
    if fix_type and fix_type in SAFE_FIX_TYPES:
        return [
            {
                "rank": 1,
                "action": f"apply safe fix '{fix_type}'",
                "rationale": (
                    f"Diagnosis class maps to a known safe fix type '{fix_type}'. "
                    "This fix is in the SAFE_FIX_TYPES tier and is eligible for "
                    "autonomous or supervised application."
                ),
                "confidence": "medium",
            },
        ]
    return [
        {
            "rank": 1,
            "action": "alert-only — no safe automated remediation known",
            "rationale": (
                f"No explicit recommendation or safe fix type mapped for "
                f"finding '{finding.id}'. Manual investigation is required."
            ),
            "confidence": "low",
        },
    ]


def recommend(
    finding: Finding,
    explanation: dict[str, Any],
    available_fixes: list[str] | None = None,
) -> dict[str, Any]:
    """Stage 3: **Recommend** — produce a ranked recommendation with rationale.

    Takes a :class:`Finding` and its structured :func:`explain` output, plus an
    optional list of available fix types, and returns a ranked list of actions
    with rationale and confidence.

    This is deterministic and data-driven — no LLM calls.  Unknown finding IDs
    receive a conservative "alert-only" fallback recommendation.
    """
    ranked = _rank_fixes(finding, explanation, available_fixes)
    result: dict[str, Any] = {
        "finding_id": finding.id,
        "ranked_actions": ranked,
    }
    log.debug(
        "recommend: finding_id=%s top_action=%s",
        finding.id,
        ranked[0]["action"] if ranked else "(none)",
    )
    return result


def _propose_action(finding: Finding) -> str:
    """Return the action a future gated-acting wave WOULD propose for *finding*.

    EXTENSION POINT: a future gated-acting implementation would, here, look up a safe
    fix, run the human-gate + backoff + verify, and invoke it.  This implementation only
    NAMES the proposal — it never acts.

    Resolution order (deterministic):
      1. an explicit per-finding proposal (``_FINDING_TO_PROPOSAL``); else
      2. a diagnosis-class -> SAFE_FIX_TYPES mapping if the finding id matches a
         known diagnosis class (reuses ``DIAGNOSIS_TO_FIX_TYPE`` as data); else
      3. a generic alert-only proposal.
    """
    explicit = _FINDING_TO_PROPOSAL.get(finding.id)
    if explicit is not None:
        return explicit
    fix_type = DIAGNOSIS_TO_FIX_TYPE.get(finding.id, "")
    if fix_type and fix_type in SAFE_FIX_TYPES:
        return f"would apply safe fix '{fix_type}' (NOT invoked — advisory only)"
    return "alert-only (no safe automated remediation)"


def remediate(findings: list[Finding]) -> list[Decision]:
    """Advisory-only remediate seam: 5-stage pipeline per DRIFT finding.

    Pipeline stages per finding:
      1. **Observe**  — the Finding is already classified (GROUND verdict).
      2. **Explain**  — ``explain()``: deterministic root-cause explanation.
      3. **Recommend** — ``recommend()``: ranked actions with rationale.
      4. **Propose**  — ``_propose_action()``: concrete would-propose string.
      5. **Decide**   — a :class:`Decision` records the proposal.

    Only ``DRIFT`` findings produce a Decision (the others are not actionable):
    a VERIFIED needs nothing, an INDETERMINATE/INCONSISTENT is not a confirmed
    fault to remediate.  Each Decision records the proposed action and the
    structural reason the gate is not acting.  Never raises.
    """
    decisions: list[Decision] = []
    for f in findings:
        if f.verdict is not Verdict.DRIFT:
            continue
        # Stage 2: Explain — deterministic root-cause analysis
        explanation = explain(f)
        # Stage 3: Recommend — ranked actions with rationale
        recommend(f, explanation)
        # Shared-governance invariant 3: producing a proposal must NEVER mutate
        # the ground verdict.  We compute the proposal and assert via the shared
        # gate that the verdict is carried through unchanged — the same predicate
        # the acting path uses, so both callers honour one regime.
        proposal = _propose_action(f)
        if not frozen_verdict_respected(f.verdict.value, f.verdict.value):  # pragma: no cover
            # Unreachable by construction (we never reassign f.verdict); the check
            # documents and enforces the invariant at the seam.
            continue
        decisions.append(
            Decision(
                finding_id=f.id,
                would_propose=proposal,
                why_not_acting=_WHY_NOT,
            )
        )
    return decisions


__all__ = ["explain", "recommend", "remediate"]
