"""backend/agent/spine_review.py — the router advisory-review interpret seam.

Implements the spine's ``interpret(findings)`` seam as an **XREF / advisory**
critic: it passes GROUND findings through the S2 deny-by-default egress boundary
to ``route_and_dispatch`` (on-host/local-first; cloud only if the user opted in),
parses the reply, and attaches advisory :class:`Annotation` notes.

**The LLM can NEVER be authority — STRUCTURAL (review R8).**  S1's
``Finding.verdict`` is frozen.  ``interpret()`` returns findings with
:class:`Annotation`s attached via ``Finding.with_annotation`` — which copies the
verdict verbatim.  An Annotation has NO verdict field and may ``raises`` at most
an ``ADVISORY_RAISEABLE`` verdict (``INCONSISTENT``), never ``VERIFIED``.  A model
reply saying "verified" for a DRIFT finding therefore CANNOT flip it — by type,
not by parser discipline.

**Egress is on the call path (R3).**  Every finding sent for review is first
projected to the allowlisted shape and verified clean by
``spine_egress.send_for_review``.  A finding that is not provably clean is NOT
sent and passes through un-annotated (fail-closed — no leak, no opinion).

**Opt-in + default-most-private.**  Review runs only if the user enabled LLM
review.  Disabled/no-key → findings pass through untouched (identical to the
default no-op ``interpret`` in spine.py).

**Cadence (cost-runaway guard).**  ``interpret()`` runs FAR less often than the
~30s GROUND ``reconcile()`` cycle: ``should_run_interpret`` gates it to a long
minimum interval AND to changed-findings-only.  The GROUND floor runs every
cycle; the (possibly cloud) review does not.
"""

from __future__ import annotations

import json
import time
from typing import Any

from backend.agent.spine import ADVISORY_RAISEABLE, Annotation, Finding, Verdict
from backend.agent.spine_egress import send_for_review
from backend.core.logging import get_logger

log = get_logger(__name__)

# Minimum seconds between interpret() runs.  Far above the ~30s GROUND cycle so a
# cloud critic cannot be invoked every cycle (cost-runaway guard).
INTERPRET_MIN_INTERVAL_S: float = 900.0  # 15 minutes

# Module-level cadence state (process-local; deliberately simple).
_last_run_at: float = 0.0
_last_findings_sig: str = ""


def _findings_signature(findings: list[Finding]) -> str:
    """A cheap signature of the (id, verdict) set — changed-findings detection."""
    return "|".join(sorted(f"{f.id}={f.verdict.value}" for f in findings))


def should_run_interpret(findings: list[Finding], *, now: float | None = None) -> bool:
    """True iff the advisory review should run THIS cycle.

    Gated by BOTH a long minimum interval AND changed-findings-only, so the
    (possibly cloud) critic runs far less often than the GROUND cycle.
    """
    global _last_run_at, _last_findings_sig
    now = time.monotonic() if now is None else now
    sig = _findings_signature(findings)
    interval_ok = (now - _last_run_at) >= INTERPRET_MIN_INTERVAL_S
    changed = sig != _last_findings_sig
    return interval_ok and changed


def _mark_ran(findings: list[Finding], *, now: float | None = None) -> None:
    global _last_run_at, _last_findings_sig
    _last_run_at = time.monotonic() if now is None else now
    _last_findings_sig = _findings_signature(findings)


def _parse_advisory(reply: str, finding: Finding) -> Annotation:
    """Parse a model reply into an ADVISORY annotation (never a verdict).

    Accepts either a JSON object ``{"note": "...", "inconsistent": bool}`` or
    free text (used verbatim as the note).  A model claim of "verified"/"fine" is
    DISCARDED as a verdict — at most we record an INCONSISTENT escalation, never a
    clearance.  Structurally cannot return anything that flips ``finding.verdict``.
    """
    note = (reply or "").strip()
    raises: Verdict | None = None
    try:
        data = json.loads(note)
        if isinstance(data, dict):
            note = str(data.get("note", "")).strip() or note
            if data.get("inconsistent") is True:
                raises = Verdict.INCONSISTENT
    except (ValueError, TypeError):
        pass  # free-text reply — keep as note
    # Defensive: even if a future change tried to honor a model verdict, the
    # Annotation constructor forbids any non-ADVISORY_RAISEABLE verdict.
    if raises is not None and raises not in ADVISORY_RAISEABLE:
        raises = None
    return Annotation(
        finding_id=finding.id,
        note=note[:500] or "(no advisory content)",
        source="llm_review",
        raises=raises,
    )


async def _review_one(
    finding: Finding, *, provider: str, dispatch_kwargs: dict[str, Any]
) -> Finding:
    """Send ONE finding through the egress boundary + router, attach an advisory.

    Returns the finding with an annotation attached on success, or unchanged if
    the egress boundary refuses (not provably clean) or the router yields nothing.
    The verdict is NEVER touched.
    """
    outcome = send_for_review(finding, provider=provider)
    if not outcome.sent or outcome.payload is None:
        # Fail-closed: not provably clean -> no send, no opinion, verdict intact.
        return finding
    try:
        from backend.agent.router.dispatch import route_and_dispatch

        prompt = json.dumps(outcome.payload, sort_keys=True)
        reply = await route_and_dispatch(prompt=prompt, **dispatch_kwargs)
    except Exception as exc:  # advisory failure must not break GROUND
        log.warning("advisory review dispatch failed", finding=finding.id, error=type(exc).__name__)
        return finding
    if not reply:
        return finding
    return finding.with_annotation(_parse_advisory(reply, finding))


async def interpret(
    findings: list[Finding],
    *,
    enabled: bool = False,
    provider: str = "ollama",
    dispatch_kwargs: dict[str, Any] | None = None,
    force: bool = False,
) -> list[Finding]:
    """Advisory XREF interpret seam.  Returns findings, possibly annotated.

    NEVER changes any verdict.  Opt-in: if ``enabled`` is False this is the
    no-op pass-through.  Cadence-gated unless ``force`` (tests).  Never raises —
    any failure degrades to the un-annotated findings.
    """
    if not enabled:
        return list(findings)
    if not force and not should_run_interpret(findings):
        return list(findings)
    _mark_ran(findings)
    if dispatch_kwargs is None:
        return list(findings)  # nothing to dispatch with — pass through

    out: list[Finding] = []
    for f in findings:
        try:
            out.append(await _review_one(f, provider=provider, dispatch_kwargs=dispatch_kwargs))
        except Exception as exc:  # per-finding isolation
            log.warning(
                "advisory review failed for finding", finding=f.id, error=type(exc).__name__
            )
            out.append(f)
    return out


__all__ = [
    "INTERPRET_MIN_INTERVAL_S",
    "interpret",
    "should_run_interpret",
]
