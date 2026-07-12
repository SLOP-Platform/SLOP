"""backend/agent/types_registry.py

Action registry type definitions — ActionSpec and the handler callable
signatures that back the Action Registry.

Imports ActionTier from types_enums (the only cross-submodule dependency).
This module adds NO executor / mutator / StateDB import so it can be read
by both the acting path AND the advisory spine without putting an action
one call away.

PINNED — these shapes are consumed by chat and the future MCP
adapter (N9); the signatures here are STABLE.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from backend.agent.types_enums import ActionTier

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
