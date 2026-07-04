"""backend/agent/policy.py — N5 tier x scope pre-approval policy (operational plan §W5).

The granular replacement for the blunt global ``OperationalLevel`` switch. A policy
answers one question for every (tier, app) pair: **may this action run WITHOUT a
human in the loop right now?** (i.e. is it *pre-approved*).

Model (operational plan §W5):

  * **global default** over ``tier`` — one decision per :class:`ActionTier`.
  * **per-app override** — a specific app may raise OR lower the global default for a
    tier (per-app blast-radius scoping, invariant 6: trust on Plex ≠ trust on the DB).

Safe defaults (operational plan §W5, "Defaults"):

  * **T0 INVESTIGATE**  → pre-approved (read-only, zero mutation).
  * **T1 REVERSIBLE**   → opt-in (NOT pre-approved by default).
  * **T2 RECOVERABLE**  → opt-in (NOT pre-approved by default; rollback + notify
                          are enforced downstream by the registry/governance gate).
  * **T3 IRREVERSIBLE** → **never pre-approvable.** A T3 is always-ask by construction
                          (safety invariant 8); no policy toggle can pre-approve it.
                          This is enforced HERE (the policy refuses to store a
                          pre-approval for T3) AND in the governance gate (a free-text
                          "do it" / pre_approved=True never satisfies a T3).

Fail-closed everywhere: a missing policy, an unreadable store, or an unknown tier
⇒ NOT pre-approved (ask, never act — safety invariant 2).

This module owns ONLY the *policy decision* (act-vs-ask authority). It does not
dispatch, does not check the rate-limit budget, and does not validate approval
tokens — those live in the shared governance gate (``backend.agent.governance``),
which this policy feeds via the ``pre_approved`` flag on ``invoke_action``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from backend.agent.types import ActionTier
from backend.core.logging import get_logger

log = get_logger(__name__)

# StateDB setting key holding the serialized policy (JSON).
_SETTING_KEY = "agent_preapproval_policy"

# Tiers that may EVER be pre-approved. T3 is excluded by construction — no policy
# toggle can make an always-ask action autonomous (safety invariant 8).
_PREAPPROVABLE_TIERS: frozenset[ActionTier] = frozenset(
    {ActionTier.INVESTIGATE, ActionTier.REVERSIBLE, ActionTier.RECOVERABLE}
)

# Safe global defaults (operational plan §W5). T0 on; T1/T2 opt-in; T3 never.
_DEFAULTS: dict[ActionTier, bool] = {
    ActionTier.INVESTIGATE: True,
    ActionTier.REVERSIBLE: False,
    ActionTier.RECOVERABLE: False,
    ActionTier.IRREVERSIBLE: False,  # immutable — kept for completeness, never True
}


@dataclass(frozen=True)
class PreApprovalPolicy:
    """The effective tier x scope pre-approval policy.

    ``global_defaults`` maps tier-int → pre-approved?. ``per_app`` maps app_key →
    {tier-int → pre-approved?}, overriding the global default for that app only.
    Both are sparse: an absent entry falls back to the safe default for that tier.
    """

    global_defaults: dict[int, bool]
    per_app: dict[str, dict[int, bool]]

    def is_pre_approved(self, tier: ActionTier, app_key: str | None) -> bool:
        """Return True iff *tier* is pre-approved for *app_key* (fail-closed).

        Resolution order: per-app override → global default → safe default. A T3
        (or any non-pre-approvable tier) is NEVER pre-approved regardless of stored
        values — the always-ask invariant cannot be toggled off.
        """
        if tier not in _PREAPPROVABLE_TIERS:
            return False
        t = int(tier)
        if app_key:
            app_over = self.per_app.get(app_key)
            if app_over is not None and t in app_over:
                return bool(app_over[t]) and tier in _PREAPPROVABLE_TIERS
        if t in self.global_defaults:
            return bool(self.global_defaults[t]) and tier in _PREAPPROVABLE_TIERS
        return _DEFAULTS.get(tier, False)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for the settings API / storage."""
        return {
            "global_defaults": {str(k): v for k, v in self.global_defaults.items()},
            "per_app": {
                app: {str(k): v for k, v in over.items()} for app, over in self.per_app.items()
            },
        }


def default_policy() -> PreApprovalPolicy:
    """The safe out-of-the-box policy (T0 on, T1/T2 opt-in, T3 never)."""
    return PreApprovalPolicy(
        global_defaults={int(t): v for t, v in _DEFAULTS.items() if t in _PREAPPROVABLE_TIERS},
        per_app={},
    )


def _coerce_tier_map(raw: Any) -> dict[int, bool]:
    """Parse a {tier: bool} mapping, dropping anything non-coercible or non-
    pre-approvable (fail-closed: bad data ⇒ omitted ⇒ falls back to safe default)."""
    out: dict[int, bool] = {}
    if not isinstance(raw, dict):
        return out
    for k, v in raw.items():
        # from_value never raises — it fail-closes to T3 on anything unrecognised,
        # which is then dropped by the pre-approvable check below.
        tier = ActionTier.from_value(k)
        if tier not in _PREAPPROVABLE_TIERS:
            continue  # never store a T3 pre-approval
        out[int(tier)] = bool(v)
    return out


def parse_policy(raw: str | None) -> PreApprovalPolicy:
    """Parse a stored policy JSON string into a :class:`PreApprovalPolicy`.

    Fail-closed: malformed/absent data ⇒ the safe default policy. Unknown or T3
    entries are dropped, never honoured.
    """
    if not raw:
        return default_policy()
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError) as exc:
        log.warning("policy: stored pre-approval policy is malformed (%s) — using safe default", exc)
        return default_policy()
    if not isinstance(data, dict):
        return default_policy()

    gdef = _coerce_tier_map(data.get("global_defaults"))
    # Backfill any unset pre-approvable tier with the safe default.
    for t, v in _DEFAULTS.items():
        if t in _PREAPPROVABLE_TIERS and int(t) not in gdef:
            gdef[int(t)] = v

    per_app_raw = data.get("per_app")
    per_app: dict[str, dict[int, bool]] = {}
    if isinstance(per_app_raw, dict):
        for app_key, over in per_app_raw.items():
            if not isinstance(app_key, str):
                continue
            coerced = _coerce_tier_map(over)
            if coerced:
                per_app[app_key] = coerced

    return PreApprovalPolicy(global_defaults=gdef, per_app=per_app)


def load_policy() -> PreApprovalPolicy:
    """Load the effective policy from StateDB; safe default on any read error."""
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            raw = db.get_setting(_SETTING_KEY)
        return parse_policy(raw)
    except Exception as exc:  # fail-closed: unreadable store ⇒ safe default
        log.warning("policy: could not load pre-approval policy (%s) — using safe default", exc)
        return default_policy()


def save_policy(policy: PreApprovalPolicy) -> None:
    """Persist *policy* to StateDB. T3 entries are already excluded by construction."""
    from backend.core.state import StateDB

    with StateDB() as db:
        db.set_setting(_SETTING_KEY, json.dumps(policy.to_dict()))
    log.info("policy: pre-approval policy updated")


def set_tier_default(tier: ActionTier, pre_approved: bool) -> PreApprovalPolicy:
    """Set the global pre-approval default for *tier* and persist. Refuses T3."""
    if tier not in _PREAPPROVABLE_TIERS:
        raise ValueError(
            f"tier {int(tier)} (T3 always-ask) can never be pre-approved (safety invariant 8)"
        )
    pol = load_policy()
    gdef = dict(pol.global_defaults)
    gdef[int(tier)] = bool(pre_approved)
    new = PreApprovalPolicy(global_defaults=gdef, per_app=dict(pol.per_app))
    save_policy(new)
    return new


def set_app_override(app_key: str, tier: ActionTier, pre_approved: bool) -> PreApprovalPolicy:
    """Set a per-app pre-approval override for (app_key, tier) and persist. Refuses T3."""
    if not app_key:
        raise ValueError("app_key is required for a per-app override")
    if tier not in _PREAPPROVABLE_TIERS:
        raise ValueError(
            f"tier {int(tier)} (T3 always-ask) can never be pre-approved (safety invariant 8)"
        )
    pol = load_policy()
    per_app = {a: dict(o) for a, o in pol.per_app.items()}
    per_app.setdefault(app_key, {})[int(tier)] = bool(pre_approved)
    new = PreApprovalPolicy(global_defaults=dict(pol.global_defaults), per_app=per_app)
    save_policy(new)
    return new


def clear_app_override(app_key: str, tier: ActionTier | None = None) -> PreApprovalPolicy:
    """Remove a per-app override (one tier, or the whole app if *tier* is None)."""
    pol = load_policy()
    per_app = {a: dict(o) for a, o in pol.per_app.items()}
    if app_key in per_app:
        if tier is None:
            per_app.pop(app_key, None)
        else:
            per_app[app_key].pop(int(tier), None)
            if not per_app[app_key]:
                per_app.pop(app_key, None)
    new = PreApprovalPolicy(global_defaults=dict(pol.global_defaults), per_app=per_app)
    save_policy(new)
    return new


def effective_policy_view() -> dict[str, Any]:
    """A UI-facing projection: per-tier effective globals + per-app overrides + the
    immutable T3-always-ask fact, so the Settings UI can surface the *effective*
    policy (operational plan §W5)."""
    pol = load_policy()
    tiers = []
    for tier in ActionTier:
        tiers.append(
            {
                "tier": int(tier),
                "name": tier.name,
                "pre_approvable": tier in _PREAPPROVABLE_TIERS,
                "global_pre_approved": pol.is_pre_approved(tier, app_key=None),
            }
        )
    return {
        "tiers": tiers,
        "per_app": pol.to_dict()["per_app"],
        "note": "T3 (irreversible/always-ask) can never be pre-approved (safety invariant 8).",
    }


__all__ = [
    "PreApprovalPolicy",
    "clear_app_override",
    "default_policy",
    "effective_policy_view",
    "load_policy",
    "parse_policy",
    "save_policy",
    "set_app_override",
    "set_tier_default",
]
