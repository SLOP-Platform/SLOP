"""backend/agent/provider_onboarding.py — AI-driven provider onboarding agent.

#991: the agent-driven provider onboarding spine.  Uses the introspection layer
from #1329 (function_registry, slot_probe, slot_conformance) to:

  1. Enumerate slots (``slot_probe.list_all_slots``)
  2. Enumerate existing providers for each slot (``registry._REGISTRY``)
  3. Identify un-filled or improvable slots
  4. Propose new provider wirings via the advisory spine (``spine.propose_slot``)
  5. Gate every proposal through OperationalLevel + runtime conformance

All proposals are advisory-only — the agent proposes, a human approves, and
the conformance gate runs at registration time.  This module answers "what
should SLOP onboard?" — it never mutates the registry directly.

LAYERING:
  * Consumes ``backend/agent/slot_probe`` (slot introspection)
  * Consumes ``backend/agent/slot_conformance`` (runtime conformance)
  * Consumes ``backend/agent/spine`` (advisory proposal type)
  * Consumes ``backend/agent/governance`` (OperationalLevel gate)
  * NEVER imports ``backend/infra/registry`` directly (cross-boundary import
    is kept minimal; uses introspection wrappers where possible).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.agent.slot_probe import list_all_slots
from backend.agent.spine import SlotProposal, propose_slot
from backend.core.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Onboarding analysis — what slots need providers?
# ---------------------------------------------------------------------------


@dataclass
class SlotAnalysis:
    """The agent's analysis of one slot's onboarding status."""

    slot: str
    slot_kind: str
    existing_providers: list[str] = field(default_factory=list)
    required_methods: tuple[str, ...] = ()
    needs_provider: bool = False
    rationale: str = ""


def _list_providers_for_slot(slot: str) -> list[str]:
    """Enumerate existing registered providers for *slot*.

    Reads from the infra registry — lazily imported to keep the
    module-level namespace free of mutator symbols."""
    try:
        from backend.infra.registry import _REGISTRY, _ensure_providers_registered

        _ensure_providers_registered()
        return sorted(key for (s, key) in _REGISTRY if s == slot)
    except Exception:
        return []


def analyze_slots() -> dict[str, SlotAnalysis]:
    """Analyze every slot for onboarding gaps.

    Returns a dict of ``slot_name → SlotAnalysis``.  A slot "needs a provider"
    if it has zero existing providers AND is a deployable slot (not selection).
    """
    result: dict[str, SlotAnalysis] = {}
    for sv in list_all_slots():
        existing = _list_providers_for_slot(sv.slot)
        needs = sv.is_deployable and len(existing) == 0
        result[sv.slot] = SlotAnalysis(
            slot=sv.slot,
            slot_kind=sv.slot_kind,
            existing_providers=existing,
            required_methods=sv.required_methods,
            needs_provider=needs,
            rationale=(
                f"slot {sv.slot!r} ({sv.slot_kind}) has no registered providers "
                f"— deployable slot must have at least one provider"
            )
            if needs
            else (f"slot {sv.slot!r} has {len(existing)} provider(s): {existing}"),
        )
    return result


def gaps() -> list[SlotAnalysis]:
    """Return analyses for slots that need a provider (un-filled deployable slots)."""
    return [a for a in analyze_slots().values() if a.needs_provider]


# ---------------------------------------------------------------------------
# Provider proposal — gated by OperationalLevel + conformance.
# ---------------------------------------------------------------------------


def propose_provider(
    slot: str,
    provider_key: str,
    *,
    operational_level: Any = None,
    rationale: str = "",
    provider_cls: type | None = None,
) -> tuple[SlotProposal | None, str]:
    """Propose a provider for *slot*, gated by OperationalLevel and conformance.

    This is the SINGLE gated entry-point for AI-driven onboarding.  It:

      1. Checks OperationalLevel — ADVISORY: log-only (proposal returned but flagged).
         SUPERVISED/AUTONOMOUS: proposal emitted.
      2. If *provider_cls* is given, runs the runtime conformance gate
         (``slot_conformance.check_provider_conformance``) and returns the
         verdict alongside the proposal.
      3. Returns ``(SlotProposal, reason)`` or ``(None, reason)`` on gate denial.

    NEVER registers anything — the proposal is advisory.  A future gated-acting
    path would consume the verified proposal and register through the conformance
    gate at registration time.

    Args:
        slot: Slot name (must be in SLOT_CONTRACTS).
        provider_key: Proposed provider key within the slot.
        operational_level: The agent's operational posture.
        rationale: Human-readable reason for the proposal.
        provider_cls: Optional provider class for runtime conformance check.

    Returns:
        (proposal or None, reason string).
    """
    operational_level = _resolve_operational_level(operational_level)

    # ADVISORY: log and return advisory-only proposal (not actionable).
    if operational_level is not None and str(operational_level) == "ADVISORY":
        log.info(
            "provider onboarding: ADVISORY mode — slot=%s key=%s (proposal only, no execution)",
            slot,
            provider_key,
        )
        return (
            propose_slot(slot, provider_key, rationale=rationale),
            "ADVISORY — proposal emitted but not actionable",
        )

    proposal = propose_slot(slot, provider_key, rationale=rationale)

    # Runtime conformance check (if a class is supplied).
    conformance_note = ""
    if provider_cls is not None:
        try:
            from backend.agent.slot_conformance import check_provider_conformance

            verdict = check_provider_conformance(slot, provider_key, provider_cls)
            if not verdict.passed:
                return (
                    proposal,
                    f"conformance FAILED: {verdict.summary} — proposal emitted but NOT verified",
                )
            conformance_note = f"conformance PASSED: {verdict.summary}"
        except Exception as exc:
            return (
                proposal,
                f"conformance check error (fail-closed): {exc} — proposal emitted but NOT verified",
            )

    reason = "proposal emitted"
    if conformance_note:
        reason += f"; {conformance_note}"

    return proposal, reason


def _resolve_operational_level(raw: Any) -> Any:
    """Resolve OperationalLevel from raw value or StateDB.  Returns None on failure."""
    if raw is not None:
        return raw
    try:
        from backend.agent.types import OperationalLevel
        from backend.core.state import StateDB

        with StateDB() as db:
            return OperationalLevel.from_setting(db.get_setting("agent_operational_level"))
    except Exception:
        return None


__all__ = [
    "SlotAnalysis",
    "analyze_slots",
    "gaps",
    "propose_provider",
]
