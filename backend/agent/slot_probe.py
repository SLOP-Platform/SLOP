"""backend/agent/slot_probe.py — agent-to-slot-introspection bridge.

The missing layer from AGENT-FULL-FUNCTIONALITY-PLAN: a bridge that lets
``backend/agent/`` reason about the slot infrastructure
(``backend/infra/slots.py``).  Before this module, zero code in ``backend/agent/``
referenced ``SLOT_CONTRACTS`` — the agent could not enumerate slots.

This module wraps the slot SSOT as read-only metadata safe for an agent (or its
advisory spine) to consume.  It never mutates slots or deploys — it answers
"what slots exist, and what does conformance look like for each one?"

LAYERING:
  * Pure metadata wrappers — imports ``backend/infra/slots`` (read-only data),
    never imports ``backend/infra/registry`` (mutator) or any provider module.
  * Safe for ``spine.py`` / advisory spine to import — no executor symbols.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.infra.slots import (
    SLOT_CONTRACTS,
    SlotContract,
    all_slots,
    deployable_slots,
    get_contract,
    selection_slots,
)
from backend.core.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Read-only projections — safe for agent introspection.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SlotView:
    """Read-only projection of a slot contract for the agent.

    Carries enough metadata for an agent to reason about slot identity,
    kind, wiring, and required methods — but NEVER carries a handler or
    provider reference.  Safe to serialize (JSON-safe types only)."""

    slot: str
    slot_kind: str
    wiring_kind: str
    cardinality: str
    required_methods: tuple[str, ...]
    has_migration: bool
    has_swap: bool
    is_selection: bool
    is_deployable: bool
    notes: str


def _to_view(c: SlotContract) -> SlotView:
    return SlotView(
        slot=c.slot,
        slot_kind=c.slot_kind,
        wiring_kind=c.wiring_kind,
        cardinality=c.cardinality,
        required_methods=c.required_methods,
        has_migration=c.migration_applies,
        has_swap=c.swap_applies,
        is_selection=(c.slot_kind == "selection"),
        is_deployable=(c.slot_kind != "selection"),
        notes=c.notes,
    )


# ---------------------------------------------------------------------------
# PINNED introspection API.
# ---------------------------------------------------------------------------


def list_all_slots() -> list[SlotView]:
    """Return every slot (deployable + selection) as read-only views."""
    return [_to_view(c) for c in SLOT_CONTRACTS.values()]


def list_deployable_slots() -> list[SlotView]:
    """Return host-mutating infra slots only (excludes selection slots)."""
    return [_to_view(SLOT_CONTRACTS[s]) for s in deployable_slots()]


def list_selection_slots() -> list[SlotView]:
    """Return registry-backed selection slots only."""
    return [_to_view(SLOT_CONTRACTS[s]) for s in selection_slots()]


def get_slot_view(slot: str) -> SlotView | None:
    """Return a single slot's view, or None if unknown."""
    try:
        return _to_view(get_contract(slot))
    except KeyError:
        return None


def slot_ids() -> list[str]:
    """All known slot ids as plain strings."""
    return list(all_slots())


def deployable_slot_ids() -> list[str]:
    """Deployable slot ids only."""
    return list(deployable_slots())


__all__ = [
    "SlotView",
    "deployable_slot_ids",
    "get_slot_view",
    "list_all_slots",
    "list_deployable_slots",
    "list_selection_slots",
    "slot_ids",
]
