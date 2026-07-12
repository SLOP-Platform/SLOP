"""backend/agent/types_governance.py

Governance gate value types — GateContext and GateOutcome.

The shared governance gate (``backend.agent.governance``) consumes these
shapes.  Every caller — scheduler, chat, MCP — presents the SAME request
shape metered through the SAME accounting (invariant 9).

Imports OperationalLevel and ActionTier from types_enums (the only
cross-submodule dependency).  No executor / mutator / StateDB imports.

PINNED — these shapes are consumed by chat and the future MCP
adapter (N9); the signatures here are STABLE.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.agent.types_enums import ActionTier, OperationalLevel


@dataclass(frozen=True)
class GateContext:
    """Inputs the shared governance gate (W2) needs to decide act-vs-deny.

    One value type so every caller — scheduler, chat, MCP — presents the SAME
    request shape and is metered through the SAME accounting (invariant 9).
    """

    action_id: str
    app_key: str
    tier: ActionTier
    operational_level: OperationalLevel
    # The authz/approval token for a conversational/MCP "do it" (invariant 8).
    # None for the scheduler (its authority is the operational level + policy).
    approval_token: str | None = None
    # True when the caller is a pre-approval-policy-backed autonomous trigger.
    pre_approved: bool = False


@dataclass(frozen=True)
class GateOutcome:
    """The shared gate's verdict. ``allow`` is the single act-vs-deny answer;
    ``reason`` is always populated (fail-closed paths explain themselves).
    """

    allow: bool
    reason: str
    # When False and the action is gated behind an explicit approval, the caller
    # should ASK rather than silently drop (fail-closed = ask, never act).
    needs_approval: bool = False
