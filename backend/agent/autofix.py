"""backend/agent/autofix.py — re-export shim.

The canonical definitions live in two sub-modules split by subsystem:
  * autofix_query   — select_auto_applicable() + AUTO_APPLICABLE_FIX_TYPES
  * autofix_execute — apply_eligible_fixes()

This file re-exports them so all existing import paths keep working.
New imports SHOULD use the sub-module path directly so tickets that
touch only one subsystem don't create spurious edit collisions.

See GOD-FILE-DECOMPOSITION-GUIDE.md for the split rationale.
"""

from backend.agent.autofix_execute import apply_eligible_fixes
from backend.agent.autofix_query import AUTO_APPLICABLE_FIX_TYPES, select_auto_applicable

__all__ = [
    "AUTO_APPLICABLE_FIX_TYPES",
    "apply_eligible_fixes",
    "select_auto_applicable",
]
