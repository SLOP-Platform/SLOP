"""backend/agent/types.py

Shared agent type definitions — re-export shim.

The canonical definitions live in three sub-modules split by subsystem:
  * types_enums       — OperationalLevel, ActionTier
  * types_registry    — ActionSpec, ActionHandler, VerifyFn, RollbackFn
  * types_governance  — GateContext, GateOutcome

This file re-exports them so all existing import paths keep working.
New imports SHOULD use the sub-module path directly so tickets that touch
only one subsystem don't create spurious edit collisions.

See GOD-FILE-DECOMPOSITION-GUIDE.md for the split rationale.
"""

from backend.agent.types_enums import ActionTier, OperationalLevel
from backend.agent.types_governance import GateContext, GateOutcome
from backend.agent.types_registry import ActionHandler, ActionSpec, RollbackFn, VerifyFn

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
