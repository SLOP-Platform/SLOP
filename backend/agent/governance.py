"""backend/agent/governance.py — the SHARED governance gate (operational plan §W2).

ONE governance regime, two callers. This module extracts the frozen-verdict +
egress-firewall + allowlist seam into an importable gate that BOTH the advisory
spine (``spine_remediate``, which stays pure/advisory) AND the acting path
(``apply.py`` via ``registry.invoke_action`` and the scheduler) call. That is
"one brain" without merging the modules — it closes structural break B1 without
deleting the import-absence test that makes advisory-only *structural*.

The shared gate is also where invariants 8 & 9 live:
  * **Invariant 9** — every registry-handler invocation (chat, MCP, OR scheduler)
    is metered through the SAME per-app/global circuit-breaker + backoff budget.
    No caller is an unmetered bypass.
  * **Invariant 8** — a conversational/MCP "do it" is an authz-bound, replay-proof,
    single-use token; a free-text "do it" NEVER satisfies a T3 always-ask.

STRUCTURAL CONSTRAINT (keeps ``test_spine_remediate`` green): this module must NOT
bind, at module scope, any executor/mutator symbol that the import-absence test
forbids (``apply_safe_fix``, ``select_auto_applicable``, ``subprocess``,
``StateDB``, ``get_fix_type``, ``record_attempt``, ``verify_container_healthy``,
``attempt_allowed``). The budget checks are imported LAZILY inside functions so
``spine_remediate`` can import this module's *advisory* helpers without any
forbidden symbol entering its namespace.
"""

from __future__ import annotations

from backend.agent.governance_advisory import egress_allowed, frozen_verdict_respected
from backend.agent.types import ActionTier, GateOutcome, OperationalLevel
from backend.core.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# The act-vs-deny authorization gate (the acting-path seam).
# ---------------------------------------------------------------------------


def authorize(
    *,
    action_id: str,
    app_key: str,
    tier: ActionTier,
    operational_level: OperationalLevel,
    approval_token: str | None = None,
    pre_approved: bool = False,
) -> GateOutcome:
    """Decide whether *action_id* may execute for *app_key*, fail-closed.

    The shared mutating-action gate. SCOPE: the autonomous agent **remediation**
    domain (self-heal / auto-fix / auto-update). As of #977/#981/#1236 EVERY known
    mutating path in that domain routes through this single chokepoint: the scheduler
    safe-fix path (scheduler.py `_authorize`), the REST apply path (api.py
    `apply_fix`), registry-invoked actions (registry.py `invoke_action`), and — the
    last remediation bypass, closed by #1236 (DToC consensus Q3 Option A,
    `.claude/run/l3-37-981-dtoc-consensus.md`) — CVE auto-heal (cve_audit.py
    `evaluate_cve_heal`, T2 RECOVERABLE / repull_restart, per-app pref as the
    pre-approval, now counted against this gate's shared budget). The chokepoint set is
    locked proven-red by `tests/test_agent_authorize_chokepoint.py` (leg 2 goes RED if a
    new authorize() call-site in backend/agent appears unregistered).

    NOT in this domain (do NOT mistake for bypasses):
      * traefik platform-continuity self-restart (`scheduler.py::_check_and_restart_traefik`)
        — ungated by design: self-heals SLOP's OWN ingress (not a catalog app), the
        agent's primary keep-SLOP-running duty, a regime distinct from app-remediation.
      * deferred install-flow app-to-app wiring (manifests/executor `run_pending_wiring`
        → wiring.py, scheduler-retried) — a USER-initiated install's plumbing (rows are
        written at install time, retried for reliability), not an autonomous remediation
        decision. Whether that retry path warrants its own governance (kill-switch /
        rate-limit) is tracked separately (#1252), out of #977 scope.

    Order (each step fails closed — deny/ask, never act):

      1. **ADVISORY** operational level ⇒ never act (deny).
      2. **T3 (irreversible / always-ask)** ⇒ requires a valid authz-bound
         approval token; a free-text "do it" / no token NEVER satisfies it
         (invariant 8). pre_approved does NOT cover T3.
      3. **Authority**: the action must be either (a) pre-approved by policy
         (supplies ``pre_approved=True`` after a tier-by-scope check), or
         (b) carry a valid approval token, or (c) be SUPERVISED-with-token. With
         none of these and a non-T0 tier ⇒ ask (needs_approval).
      4. **Shared budget** (invariant 9): the per-app + global circuit-breaker
         budget must be OPEN. Checked LAST so a denied/asked action never spends
         budget. Imported lazily.

    Returns a :class:`GateOutcome` — ``allow`` is the single answer.
    """
    # (1) ADVISORY never acts.
    if operational_level is OperationalLevel.ADVISORY:
        return GateOutcome(
            allow=False,
            reason=f"operational level ADVISORY — '{action_id}' proposed, not executed",
        )

    # (2) Authority. A single authority decision (token is single-use, so it is
    #     validated/consumed AT MOST ONCE here):
    #       * T3 (always-ask): ONLY a valid authz-bound token authorizes; neither
    #         pre-approval nor a free-text "do it" ever satisfies a T3 (inv. 8).
    #       * T1/T2: pre-approval (policy) OR a valid token authorizes.
    #       * T0: no authority needed (read-only).
    if tier >= ActionTier.IRREVERSIBLE:
        if not _approval_token_valid(approval_token, action_id=action_id, app_key=app_key):
            return GateOutcome(
                allow=False,
                needs_approval=True,
                reason=(
                    f"'{action_id}' is T3 (irreversible/always-ask): requires a valid "
                    "single-use approval token — pre-approval and free-text 'do it' "
                    "never satisfy a T3 (invariant 8)"
                ),
            )
    elif tier >= ActionTier.REVERSIBLE and not pre_approved:
        if not _approval_token_valid(approval_token, action_id=action_id, app_key=app_key):
            return GateOutcome(
                allow=False,
                needs_approval=True,
                reason=(
                    f"'{action_id}' (tier {int(tier)}) is not pre-approved and carries no "
                    "valid approval token — ask before acting (fail-closed)"
                ),
            )

    # (3) Shared rate-limit budget — checked last (no spend on a denied action).
    budget_ok, budget_reason = _budget_open(app_key)
    if not budget_ok:
        return GateOutcome(allow=False, reason=budget_reason)

    return GateOutcome(allow=True, reason="authorized")


# ---------------------------------------------------------------------------
# Internal helpers — lazy imports keep forbidden symbols out of module scope.
# ---------------------------------------------------------------------------


def _approval_token_valid(token: str | None, *, action_id: str, app_key: str) -> bool:
    """Validate an authz-bound, replay-proof, single-use approval token.

    Invariant 8. Delegates to the control-plane token store
    lazily. In the MVP keystone commit the token store is the A0 auth module; a
    token is valid iff it is recognised, bound to (action_id, app_key), unexpired,
    and not yet consumed. Fail-closed: any resolution error ⇒ invalid.
    """
    if not token:
        return False
    try:
        from backend.agent.approval import consume_approval_token

        return bool(consume_approval_token(token, action_id=action_id, app_key=app_key))
    except Exception as exc:  # fail-closed: cannot validate ⇒ invalid
        log.debug("governance: approval token validation failed: %s", exc)
        return False


def _budget_open(app_key: str) -> tuple[bool, str]:
    """Invariant 9: check the SAME per-app + global circuit-breaker budget every
    caller shares. Lazy import keeps the breaker out of module scope. Fail-closed:
    a closed breaker (incl. db_unreachable) denies."""
    try:
        from backend.agent.circuit_breaker import check_app_circuit, check_circuit

        glob = check_circuit(cap=10)
        if not glob.open:
            return (
                False,
                f"global autofix budget closed ({glob.reason}: {glob.fixes_last_hour}/{glob.cap})",
            )
        per_app = check_app_circuit(app_key, cap=5)
        if not per_app.open:
            return (
                False,
                f"per-app autofix budget closed for {app_key} "
                f"({per_app.reason}: {per_app.fixes_last_hour}/{per_app.cap})",
            )
        return True, "budget open"
    except Exception as exc:  # fail-closed
        log.warning("governance._budget_open: budget check failed for %s: %s", app_key, exc)
        return False, f"budget check error (fail-closed): {exc}"


__all__ = [
    "authorize",
    "egress_allowed",
    "frozen_verdict_respected",
]
