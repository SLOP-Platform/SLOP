"""backend/agent/spine.py — the reusable agent-oversight spine contract.

This is the **PINNED open API** every agent-expansion stratum plugs into.  It
encodes the one shape from ``docs/AGENT-EXPANSION-SURVEY.md`` §0:

    GROUND probe that can go red against physics
      -> optional advisory interpretation (LLM = XREF/advisory, never authoritative)
      -> bounded, human-gated remediation.

The three protocol seams are:

  * ``reconcile() -> list[Finding]``     — GROUND.  Observes physics only.  Emits
    frozen-verdict :class:`Finding` objects.  ``backend/agent/self_audit.py`` is
    the reference implementation.
  * ``interpret(findings) -> list[Finding]`` — XREF / advisory.  Default is a
    no-op pass-through.  An interpreter (S3 ``spine_review.py``) may only ATTACH
    :class:`Annotation` notes; it can NEVER write ``Finding.verdict`` (the verdict
    is frozen — see below).  The LLM explains, it does not decide.
  * ``remediate(findings) -> list[Decision]`` — bounded remediation.  The
    current implementation (``spine_remediate.py``) is **advisory-only**: it
    returns :class:`Decision` objects describing what it WOULD propose, wiring no
    action.

Two-owner firewall (HARD): every consumer of this contract is RUNTIME-ONLY — it
observes the live process / OS / Docker / DB and the manifest the agent already
reads, and NEVER reads docs/process/runbooks to decide anything.

PINNED: S2/S3/S4 and every future stratum *import* the symbols below.  They do
NOT edit this module's logic — the frozen-verdict guarantee and the egress-seam
location are load-bearing safety properties of this contract.

The egress trust boundary (``send_for_review``) is OWNED by S2
(``spine_egress.py``); this module pins its import location and re-exports it so
consumers have a single import site, but does not implement it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from collections.abc import Callable

from backend.core.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pinned vocabulary (CLAUDE.md "Knowledge-Lifecycle", verbatim)
# ---------------------------------------------------------------------------


class Verdict(StrEnum):
    """The four GROUND verdicts.  A green light is only trustworthy if it can go
    red against physics — hence VERIFIED is just one of four, and an unreachable
    ground source yields INDETERMINATE (loud), never a silent VERIFIED."""

    VERIFIED = "verified"
    DRIFT = "DRIFT"
    INCONSISTENT = "INCONSISTENT"
    INDETERMINATE = "INDETERMINATE"


# Verdicts an advisory (XREF) interpreter is permitted to RAISE.  It may flag an
# INCONSISTENT (a cross-reference disagreement) but can never assert VERIFIED and
# can never clear a DRIFT.  This set is referenced by S3; it is part of the
# contract that the LLM is advisory, never authoritative.
ADVISORY_RAISEABLE: frozenset[Verdict] = frozenset({Verdict.INCONSISTENT})


@dataclass(frozen=True)
class Annotation:
    """An advisory (XREF) note attached by ``interpret()``.

    STRUCTURAL guarantee (R8): an Annotation has NO ``verdict`` field and is a
    DISTINCT type from :class:`Finding`.  An interpreter therefore cannot — by
    type, not by parser discipline — flip a finding's verdict.  The most it can
    do is attach a note or signal ``raises=Verdict.INCONSISTENT``.
    """

    finding_id: str
    note: str
    source: str = "advisory"
    # An interpreter may RAISE only an ADVISORY_RAISEABLE verdict (INCONSISTENT);
    # None means "no escalation, note only".  It can never carry VERIFIED.
    raises: Verdict | None = None

    def __post_init__(self) -> None:
        if self.raises is not None and self.raises not in ADVISORY_RAISEABLE:
            raise ValueError(
                f"advisory annotation may only raise {sorted(v.value for v in ADVISORY_RAISEABLE)}; "
                f"got {self.raises!r} — the LLM is XREF/advisory, never authoritative"
            )


@dataclass(frozen=True)
class Finding:
    """A single GROUND observation reconciled against physics.

    ``verdict`` is **set once and frozen** (the dataclass is ``frozen=True``):
    once ``reconcile()`` decides a DRIFT, nothing downstream — no interpreter, no
    model reply — can mutate it.  ``annotations`` carries advisory XREF notes that
    ride ALONGSIDE the verdict without touching it.
    """

    id: str
    physics: str  # one-line description of the ground source it probed
    verdict: Verdict
    summary: str  # fixed-vocabulary, allowlist-safe one-liner
    detail: str = ""  # human-readable; NEVER cloud-bound unless allowlisted
    annotations: tuple[Annotation, ...] = field(default_factory=tuple)

    def with_annotation(self, ann: Annotation) -> Finding:
        """Return a COPY carrying an additional advisory annotation.

        The verdict is preserved verbatim — this is the ONLY sanctioned way an
        interpreter records its (advisory) opinion, and it provably cannot change
        the verdict because a frozen dataclass forbids in-place mutation and this
        method copies ``verdict`` unchanged.
        """
        if ann.finding_id != self.id:
            raise ValueError(f"annotation.finding_id {ann.finding_id!r} != finding.id {self.id!r}")
        return Finding(
            id=self.id,
            physics=self.physics,
            verdict=self.verdict,  # frozen — copied, never recomputed
            summary=self.summary,
            detail=self.detail,
            annotations=(*self.annotations, ann),
        )


@dataclass(frozen=True)
class Decision:
    """The advisory-only output of ``remediate()``.

    Describes the action a future gated-acting implementation WOULD propose, with no action
    wired.  ``why_not_acting`` records the structural reason it is inert.
    """

    finding_id: str
    would_propose: str
    why_not_acting: str = "advisory-only spine; no auto-remediation wired"


# ---------------------------------------------------------------------------
# Slot-proposal type (AGENT-FUNC spine seam — #1329).
# ---------------------------------------------------------------------------
# An agent performing AI-driven provider onboarding (#991) emits proposals
# through the advisory spine.  A ``SlotProposal`` is the structured type that
# represents "for slot X, propose provider Y with this wiring."  Like
# ``Decision`` it is advisory-only — no action is wired.  A future gated-acting
# implementation would CONSUME a verified Proposal and route it through the
# conformance gate + OperationalLevel before registering.
#
# The type lives here in the spine contract so it is importable by every
# stratum without pulling in slot or provider code.


@dataclass(frozen=True)
class SlotProposal:
    """Advisory-only slot provider proposal emitted by the agent spine.

    Describes the wiring a future AI-driven onboarding agent (#991) WOULD
    propose for a slot, with no action wired.  ``rationale`` records the
    agent's reasoning; ``conformance_expected`` is a human-readability field
    (the actual conformance check runs at registration time, not in the
    proposal)."""

    slot: str
    provider_key: str
    rationale: str = ""
    conformance_expected: str = "unverified — advisory proposal only"


def propose_slot(slot: str, provider_key: str, *, rationale: str = "") -> SlotProposal:
    """Create an advisory-only slot provider proposal.

    The PINNED seam for agent-driven provider onboarding (#991).  Returns a
    ``SlotProposal`` that describes the proposed wiring; the proposal is
    advisory and unverified.  A future gated-acting path would run conformance
    BEFORE registering — this seam is the advisory half.
    """
    return SlotProposal(
        slot=slot,
        provider_key=provider_key,
        rationale=rationale,
    )


# ---------------------------------------------------------------------------
# Protocol seam signatures (the three reusable seams)
# ---------------------------------------------------------------------------

# A reconciler is any GROUND callable returning Findings (self_audit.reconcile).
Reconciler = Callable[[], "list[Finding]"]
# An interpreter attaches advisory annotations; default is the no-op below.
Interpreter = Callable[["list[Finding]"], "list[Finding]"]
# A remediator maps findings to advisory-only Decisions.
Remediator = Callable[["list[Finding]"], "list[Decision]"]
# A proposer emits advisory-only SlotProposals for provider onboarding (#991).
Proposer = Callable[[str, str], "SlotProposal"]


def interpret(findings: list[Finding]) -> list[Finding]:
    """Default interpret seam: a no-op pass-through (no LLM, no annotations).

    S3 (``spine_review.py``) provides the real, opt-in XREF interpreter.  The
    default keeps the GROUND verdicts untouched so a deployment with LLM review
    disabled behaves identically minus the advisory notes.
    """
    return list(findings)


# ---------------------------------------------------------------------------
# Egress trust boundary — pinned location; IMPLEMENTED by S2 (spine_egress.py)
# ---------------------------------------------------------------------------
#
# Every cloud-bound payload passes through ``backend.agent.spine_egress.send_for_review``.
# S1 pins the import site; S2 owns the deny-by-default allowlist + fail-closed logic.
# Consumers import it from spine_egress directly (see S2/S3 deliverables); this
# comment is the contract pin so the boundary location does not drift.


# ---------------------------------------------------------------------------
# Cycle entry point — called from run_health_cycle() (never raises)
# ---------------------------------------------------------------------------


VERDICT_TO_HEALTH_STATUS: dict[Verdict, str] = {
    Verdict.VERIFIED: "ok",
    Verdict.DRIFT: "critical",
    Verdict.INCONSISTENT: "degraded",
    Verdict.INDETERMINATE: "unknown",
}

HEALTH_SUBJECT_TYPE: str = "agent_self_audit"


def persist_findings(findings: list[Finding], db_factory: Callable[[], Any]) -> None:
    """Persist findings to health_checks via ``db_factory`` (a StateDB context mgr).

    Extracted from the checker hook so the cycle-insertion site stays minimal and
    every stratum writes the same row shape.  ``db_factory()`` yields an object
    supporting ``upsert_health_check(...)`` as a context manager.
    """
    with db_factory() as db:
        for f in findings:
            db.upsert_health_check(
                subject_type=HEALTH_SUBJECT_TYPE,
                subject_key=f.id,
                check_name="self_audit",
                status=VERDICT_TO_HEALTH_STATUS.get(f.verdict, "unknown"),
                summary=f.summary,
                detail=f.detail,
            )


def run_self_audit_cycle(
    reconcile: Reconciler | None = None,
    *,
    persist: Callable[[list[Finding]], None] | None = None,
) -> list[Finding]:
    """Run one GROUND self-audit cycle.  NEVER raises.

    This is the seam ``backend/health/checker.run_health_cycle()`` invokes each
    cycle.  A spine failure degrades to a single recorded INDETERMINATE finding —
    it must never break the health cycle (survey §4 never-raises contract).

    ``reconcile`` defaults to the self-audit reference reconciler.  ``persist``,
    if given, writes the findings (the checker passes a StateDB-backed writer);
    a persist failure is swallowed and logged, never propagated.
    """
    try:
        if reconcile is None:
            from backend.agent.self_audit import reconcile as _default_reconcile

            reconcile = _default_reconcile
        findings = reconcile()
    except Exception as exc:  # the cycle must never break
        log.warning("self-audit reconcile failed: %s", exc)
        findings = [
            Finding(
                id="self_audit.cycle",
                physics="self-audit reconcile() invocation",
                verdict=Verdict.INDETERMINATE,
                summary="self-audit cycle failed to run",
                detail=f"reconcile raised: {type(exc).__name__}",
            )
        ]

    if persist is not None:
        try:
            persist(findings)
        except Exception as exc:  # persistence is best-effort
            log.warning("self-audit persist failed: %s", exc)

    return findings


__all__ = [
    "ADVISORY_RAISEABLE",
    "HEALTH_SUBJECT_TYPE",
    "VERDICT_TO_HEALTH_STATUS",
    "Annotation",
    "Decision",
    "Finding",
    "Interpreter",
    "Proposer",
    "Reconciler",
    "Remediator",
    "SlotProposal",
    "Verdict",
    "interpret",
    "persist_findings",
    "propose_slot",
    "run_self_audit_cycle",
]
