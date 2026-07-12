"""backend/agent/types_enums.py

Agent enum definitions — OperationalLevel and ActionTier.

Zero external imports. These are the foundation types used by all other
agent type modules.  Separated so tickets that add autonomy levels (#488)
or extend the tier ladder touch ONLY this file, leaving the governance and
registry types untouched.

PINNED — these enums are consumed by chat and the future MCP
adapter (N9); the values and parsers here are STABLE.
"""

from __future__ import annotations

import enum


class OperationalLevel(enum.Enum):
    """Controls how much autonomy the auto-apply pipeline exercises.

    OBSERVE:     Detection only — findings are recorded, never acted upon.  Level 0.
    ADVISORY:    The pipeline only reports what it *would* do — no mutations.  Level 1.
    RECOMMEND:   Findings include a recommended action, but execution requires
                 human approval (equivalent to ADVISORY in the execution gate).  Level 2.
    SUPERVISED:  Mutations require the confirmation gate (dry_run=True by default).
                 The caller must explicitly pass dry_run=False to execute.  Level 3.
    AUTONOMOUS:  Gate bypassed; actions execute immediately. Only valid when
                 explicitly configured via settings (agent_operational_level=autonomous).  Level 4.
    """

    OBSERVE = "observe"
    ADVISORY = "advisory"
    RECOMMEND = "recommend"
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
        most dangerous tier (T3 always-ask) on anything unrecognised.
        """
        if isinstance(raw, ActionTier):
            return raw
        try:
            if isinstance(raw, str) and not raw.isdigit():
                return cls[raw.strip().upper()]
            return cls(int(raw))
        except (KeyError, ValueError):
            return cls.IRREVERSIBLE
