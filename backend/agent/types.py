"""backend/agent/types.py

Shared agent type definitions — the PINNED contracts for the Action Registry.

Separated here to avoid circular imports between autofix, apply, and spine
modules.  This module imports NO executor / mutator / StateDB symbol so it can be
read by *both* the acting path (apply/autofix/scheduler) AND the advisory spine
without putting an action one call away.

PINNED — these shapes are consumed by chat
 and the future MCP adapter; the signatures here are STABLE:

  * ``OperationalLevel`` — coarse autonomy posture (kept from).
  * ``ActionTier``        — 0..3 blast-radius ladder (the tier ladder).
  * ``ActionSpec``        — one declarative registry entry: every executable
                            action declares id, tier, reversible, handler,
                            verify_fn, rollback_fn, default_rate_limit, scopeable.
  * ``GateContext`` / ``GateOutcome`` — the value types the shared governance gate
                            (``backend.agent.governance``) consumes/returns.
"""

from __future__ import annotations

import enum
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any


class OperationalLevel(enum.Enum):
    """Controls how much autonomy the auto-apply pipeline exercises.

    ADVISORY:    The pipeline only reports what it *would* do — no mutations.
    SUPERVISED:  Mutations require the confirmation gate (dry_run=True by default).
                 The caller must explicitly pass dry_run=False to execute.
    AUTONOMOUS:  Gate bypassed; actions execute immediately. Only valid when
                 explicitly configured via settings (agent_operational_level=autonomous).
    """

    ADVISORY = "advisory"
    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"

    @classmethod
    def from_setting(cls, raw: str | None) -> OperationalLevel:
        """Parse an operational level from a settings string (case-insensitive).

        Defaults to SUPERVISED when the setting is absent or unrecognised — the
        safest non-advisory mode that still allows opt-in execution.
        """
        if raw and raw.strip().lower() in {m.value for m in cls}:
            return cls(raw.strip().lower())
        return cls.SUPERVISED


class ActionTier(enum.IntEnum):
    """Blast-radius tier ladder for a registry action (operational plan §W1).

    The numeric value IS the blast radius — higher means more dangerous, so
    ``tier >= T2`` style comparisons express "this much risk or worse".

    T0 INVESTIGATE  — read-only (probe, pull logs, assemble context,
                      escalate-for-diagnosis). Zero mutation.
    T1 REVERSIBLE   — reversible fix (``restart_container``). Backoff + breaker +
                      verify already guard it.
    T2 RECOVERABLE  — recoverable fix (``repull_restart``,
                      ``restart_managed_service``). Rollback + notify REQUIRED.
    T3 IRREVERSIBLE — irreversible / data-touching (``remount_storage``,
                      env/config edits). ALWAYS-ASK (free-text "do it" never
                      satisfies a T3; see invariant 8).
    """

    INVESTIGATE = 0
    REVERSIBLE = 1
    RECOVERABLE = 2
    IRREVERSIBLE = 3

    @classmethod
    def from_value(cls, raw: int | str | ActionTier) -> ActionTier:
        """Coerce an int / name / member to an ActionTier; fail-closed to the
        most dangerous tier (T3 always-ask) on anything unrecognised."""
        if isinstance(raw, ActionTier):
            return raw
        try:
            if isinstance(raw, str) and not raw.isdigit():
                return cls[raw.strip().upper()]
            return cls(int(raw))
        except (KeyError, ValueError):
            return cls.IRREVERSIBLE


# Handler / verify / rollback callables operate on (app_key, params) and return a
# result dict ({"ok": bool, "message": str, ...}). Kept as broad Callables so the
# registry can reference apply.py handlers without importing them here (no cycle).
ActionHandler = Callable[..., dict[str, Any]]
VerifyFn = Callable[..., tuple[bool, str]]
RollbackFn = Callable[..., dict[str, Any]]


@dataclass(frozen=True)
class ActionSpec:
    """One declarative entry in the Action Registry — the keystone.

    Every executable action declares itself here ONCE; pre-approval policy, the
    governed mutation path, chat dispatch, and the future MCP surface all read
    from this single record. Adding an action later = one ActionSpec, which then
    AUTOMATICALLY inherits a tier, the pre-approval policy, verify+rollback, rate
    limits, and notify — so "someone forgot to gate the new action" becomes
    impossible by construction (enforced by the W1 meta-gate test).

    Fields (PINNED):
      id                 — stable action id (e.g. ``restart_container``). Also the
                           fix_type when this action is an auto-fix.
      tier               — ActionTier (0..3 blast radius).
      reversible         — True iff the action can be undone (T1) or auto-rolled-
                           back (T2). T3 actions are not reversible.
      handler            — the executor callable, or None for declared-but-pending
                           actions (still gated; cannot fire).
      verify_fn          — post-action health verification, or None.
      rollback_fn        — undo/rollback callable (REQUIRED for T2), or None.
      default_rate_limit — per-hour cap fed to the shared circuit-breaker budget.
      scopeable          — True iff pre-approval may be granted per-app (vs global
                           only). Per-app blast-radius scoping (invariant 6).
      diagnosis_classes  — diagnosis_class values that map to this action (the
                           registry-derived DIAGNOSIS_TO_FIX_TYPE source).
      description        — one-line human/LLM-facing description (chat + MCP).
    """

    id: str
    tier: ActionTier
    reversible: bool
    handler: ActionHandler | None = None
    verify_fn: VerifyFn | None = None
    rollback_fn: RollbackFn | None = None
    default_rate_limit: int = 5
    scopeable: bool = True
    diagnosis_classes: tuple[str, ...] = field(default_factory=tuple)
    description: str = ""

    @property
    def executable(self) -> bool:
        """True iff this action has a wired handler (can actually mutate)."""
        return self.handler is not None


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
    ``reason`` is always populated (fail-closed paths explain themselves)."""

    allow: bool
    reason: str
    # When False and the action is gated behind an explicit approval, the caller
    # should ASK rather than silently drop (fail-closed = ask, never act).
    needs_approval: bool = False


__all__ = [
    "ActionHandler",
    "ActionSpec",
    "ActionTier",
    "GateContext",
    "GateOutcome",
    "OperationalLevel",
    "RollbackFn",
    "VerifyFn",
]
