"""backend/agent/governance_advisory.py — advisory-safe governance helpers.

The advisory spine (``spine_remediate``) can import these without any executor
or mutator symbol entering its namespace.  These are pure functions: string
comparison, a lazy allowlist lookup.  No acting-path symbols.

Separated from ``governance.py`` so tickets touching the acting-path gate
(authorize) do NOT collide with features that consume only the advisory
subset.  The import statement is the structural guarantee: the advisory
spine imports from THIS module, not from ``governance.py``, so its
import-absence test stays trivially green.
"""

from __future__ import annotations

from backend.core.logging import get_logger

log = get_logger(__name__)


# ── frozen-verdict guard ───────────────────────────────────────────────


def frozen_verdict_respected(original: str, proposed: str) -> bool:
    """Invariant 3: a downstream stage may never MUTATE a ground verdict.

    Returns True iff *proposed* equals *original* (the verdict was carried through
    unchanged). Pure string comparison — binds no executor; safe for the advisory
    spine to import. Callers that detect ``False`` must refuse to act.
    """
    return original == proposed


# ── egress / allowlist seam ────────────────────────────────────────────


def egress_allowed(field_name: str) -> bool:
    """Allowlist check for a cloud-bound field name (deny-by-default).

    Thin wrapper over the existing egress firewall (``spine_egress``); imported
    lazily so this module stays free of the firewall's transitive imports. Any
    failure to resolve the allowlist fails CLOSED (returns False).
    """
    try:
        from backend.agent.spine_egress import ALLOWED_KEYS

        return field_name in ALLOWED_KEYS
    except Exception as exc:  # fail-closed: unknown ⇒ deny egress
        log.debug("governance_advisory.egress_allowed: allowlist resolve failed for %s: %s", field_name, exc)
        return False


__all__ = [
    "egress_allowed",
    "frozen_verdict_respected",
]
