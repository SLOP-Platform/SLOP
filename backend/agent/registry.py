"""backend/agent/registry.py — the Action Registry keystone + tool-surface seam.

This is the **single source of truth** for every action the agent can take. It
realizes operational-plan §W1: each action is one declarative :class:`ActionSpec`
(id, tier, reversible, handler, verify_fn, rollback_fn, default_rate_limit,
scopeable). Pre-approval policy, the governed mutation path
(``governance`` + ``apply``), chat dispatch, and the future MCP
adapter (N9) ALL read from this one registry.

PINNED tool-surface seam — STABLE signatures
consumed by chat and the MCP adapter:

    list_actions() -> list[ActionView]
    invoke_action(action_id, params, approval_token=None, *, app_key, ...) -> dict

``invoke_action`` enforces tier + policy + the shared governance gate
(``backend.agent.governance``) before dispatching to the spec's handler. No
caller dispatches a handler directly; every mutation routes through here (or the
scheduler, which routes through the same shared gate — invariant 9).

LAYERING (cycle-free):
  * The pure *metadata* table (``_SPEC_META``) carries tier/reversibility/
    diagnosis-class mapping and imports NOTHING from apply. ``apply.py`` derives
    its ``SAFE_FIX_TYPES`` / ``DIAGNOSIS_TO_FIX_TYPE`` from this metadata, so the
    taxonomy is registry-derived (N1) with no import cycle.
  * Handlers/verify/rollback are attached lazily (``build_registry()``), which
    imports apply's executors only when the WIRED registry is first requested —
    so importing this module for metadata never pulls in an executor.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.agent.types import ActionSpec, ActionTier
from backend.core.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Pure metadata layer (NO executor import) — registry-derived taxonomy source.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SpecMeta:
    """Handler-free description of a registry action: everything apply.py needs to
    derive the taxonomy, without importing an executor."""

    id: str
    tier: ActionTier
    reversible: bool
    default_rate_limit: int
    scopeable: bool
    diagnosis_classes: tuple[str, ...]
    description: str
    # Name of the apply.py handler attribute, or None for declared-but-pending.
    handler_name: str | None
    verify_name: str | None
    rollback_name: str | None


# The canonical action table. Adding an action = one row here.
# T0 read-only actions (investigate/escalate) carry no handler in the MVP — they
# are declared so the meta-gate accounts for the whole vocabulary, but the
# read-only path does not flow through apply.py.
_SPEC_META: tuple[_SpecMeta, ...] = (
    _SpecMeta(
        id="restart_container",
        tier=ActionTier.REVERSIBLE,  # T1
        reversible=True,
        default_rate_limit=5,
        scopeable=True,
        diagnosis_classes=("CRASH_LOOP", "HEALTHCHECK_TIMEOUT", "DEPENDENCY_DOWN"),
        description="Restart a managed container (docker restart).",
        handler_name="_restart_container",
        verify_name="verify_container_healthy",
        rollback_name=None,  # restart is self-reversible; no separate rollback
    ),
    _SpecMeta(
        id="repull_restart",
        tier=ActionTier.RECOVERABLE,  # T2
        reversible=True,
        default_rate_limit=5,
        scopeable=True,
        diagnosis_classes=("IMAGE_PULL_FAIL",),
        description="Re-pull the image and restart; auto-rollback on failed verify.",
        handler_name="_repull_restart",
        verify_name="verify_container_healthy",
        rollback_name="safe_update_container",  # safe_update.py rollback leg
    ),
    _SpecMeta(
        id="restart_managed_service",
        tier=ActionTier.RECOVERABLE,  # T2
        reversible=True,
        default_rate_limit=5,
        scopeable=True,
        diagnosis_classes=(),  # routed by intent, not a diagnosis_class today
        description="Restart a managed service unit with backup→verify→rollback.",
        handler_name="_restart_managed_service",
        verify_name="verify_container_healthy",
        rollback_name="safe_update_container",
    ),
    # env_var_format is a declared-but-PENDING T3 (Phase H stub): it is in the
    # vocabulary and gated, but has NO wired handler, so it can never fire.
    _SpecMeta(
        id="env_var_format",
        tier=ActionTier.IRREVERSIBLE,  # T3 always-ask
        reversible=False,
        default_rate_limit=1,
        scopeable=False,
        diagnosis_classes=("UNRESOLVED_PLACEHOLDER",),
        description="Edit an env/config value (Phase H — not yet executable).",
        handler_name=None,
        verify_name=None,
        rollback_name=None,
    ),
)

_META_BY_ID: dict[str, _SpecMeta] = {m.id: m for m in _SPEC_META}


# ---------------------------------------------------------------------------
# Registry-derived taxonomy (N1) — consumed by apply.py (no import cycle).
# ---------------------------------------------------------------------------


def safe_fix_types() -> set[str]:
    """The set of fix_type ids the safe-apply tier knows about (T1/T2 + the
    declared env_var_format stub) — derived from the registry metadata so the
    taxonomy can never drift from the registry (N1)."""
    return {m.id for m in _SPEC_META if m.diagnosis_classes or m.handler_name}


def diagnosis_to_fix_type() -> dict[str, str]:
    """Map diagnosis_class → fix_type id, derived from the registry metadata.
    Single source of truth for the taxonomy (replaces the hand-maintained dict)."""
    out: dict[str, str] = {}
    for m in _SPEC_META:
        for dc in m.diagnosis_classes:
            out[dc] = m.id
    return out


def executable_action_ids() -> set[str]:
    """Ids of actions with a wired handler (can actually mutate)."""
    return {m.id for m in _SPEC_META if m.handler_name is not None}


def all_spec_meta() -> tuple[_SpecMeta, ...]:
    """The raw metadata table (handler-free) — used by the meta-gate test."""
    return _SPEC_META


def tier_for(action_id: str) -> ActionTier:
    """Tier for *action_id*; fail-closed to T3 (always-ask) for an unknown id."""
    m = _META_BY_ID.get(action_id)
    return m.tier if m else ActionTier.IRREVERSIBLE


# ---------------------------------------------------------------------------
# Wired registry (handlers attached) — built lazily to avoid an import cycle.
# ---------------------------------------------------------------------------

_WIRED: dict[str, ActionSpec] | None = None


def build_registry() -> dict[str, ActionSpec]:
    """Return the WIRED registry {id: ActionSpec}, attaching real handlers.

    Imports apply.py executors lazily (function-local) so merely importing this
    module for metadata never pulls an executor into scope. Cached after first
    build.
    """
    global _WIRED
    if _WIRED is not None:
        return _WIRED

    # Lazy executor imports — confined to this function.
    from backend.agent import apply as _apply
    from backend.agent.safe_update import safe_update_container
    from backend.agent.verify import verify_container_healthy

    # Adapt apply.py executors to the uniform (app_key, params) seam signature.
    def _h_restart(app_key: str, params: dict[str, object]) -> dict[str, object]:
        return _apply._restart_container(app_key)

    def _h_repull(app_key: str, params: dict[str, object]) -> dict[str, object]:
        meta = params.get("fix_metadata") if isinstance(params, dict) else None
        return _apply._repull_restart(app_key, meta if isinstance(meta, dict) else {})

    def _h_restart_svc(app_key: str, params: dict[str, object]) -> dict[str, object]:
        return _apply._restart_managed_service(app_key, params if isinstance(params, dict) else {})

    handler_table = {
        "_restart_container": _h_restart,
        "_repull_restart": _h_repull,
        "_restart_managed_service": _h_restart_svc,
    }
    verify_table = {"verify_container_healthy": verify_container_healthy}
    rollback_table = {"safe_update_container": safe_update_container}

    wired: dict[str, ActionSpec] = {}
    for m in _SPEC_META:
        wired[m.id] = ActionSpec(
            id=m.id,
            tier=m.tier,
            reversible=m.reversible,
            handler=handler_table.get(m.handler_name) if m.handler_name else None,
            verify_fn=verify_table.get(m.verify_name) if m.verify_name else None,
            rollback_fn=rollback_table.get(m.rollback_name) if m.rollback_name else None,
            default_rate_limit=m.default_rate_limit,
            scopeable=m.scopeable,
            diagnosis_classes=m.diagnosis_classes,
            description=m.description,
        )
    _WIRED = wired
    return _WIRED


# ---------------------------------------------------------------------------
# PINNED tool-surface seam.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ActionView:
    """The read-only projection of a registry action for clients (chat / MCP).
    Never carries the handler callable — clients see metadata, not executors."""

    id: str
    tier: int
    reversible: bool
    executable: bool
    scopeable: bool
    default_rate_limit: int
    diagnosis_classes: tuple[str, ...]
    description: str


def list_actions() -> list[ActionView]:
    """PINNED. Return the full action vocabulary as read-only views.

    Consumed by chat and the MCP adapter. Lists EVERY declared
    action — including declared-but-pending ones — so a client can present the
    whole surface and its tiers. Carries no handler reference.
    """
    reg = build_registry()
    return [
        ActionView(
            id=spec.id,
            tier=int(spec.tier),
            reversible=spec.reversible,
            executable=spec.executable,
            scopeable=spec.scopeable,
            default_rate_limit=spec.default_rate_limit,
            diagnosis_classes=spec.diagnosis_classes,
            description=spec.description,
        )
        for spec in reg.values()
    ]


def get_spec(action_id: str) -> ActionSpec | None:
    """Return the wired ActionSpec for *action_id*, or None if unknown."""
    return build_registry().get(action_id)


def invoke_action(
    action_id: str,
    params: dict[str, object] | None = None,
    approval_token: str | None = None,
    *,
    app_key: str,
    operational_level: object | None = None,
    pre_approved: bool = False,
) -> dict[str, object]:
    """PINNED. The SINGLE gated entry-point to take an action.

    Chat and the future MCP adapter both call this; neither
    dispatches a handler directly. Enforces, in order (fail-closed at each step):

      1. **Known action.** Unknown ``action_id`` ⇒ deny (never invent an action).
      2. **Executable.** A declared-but-pending action (no handler) ⇒ deny.
      3. **Shared governance gate** (``backend.agent.governance.authorize``):
         tier x policy x approval-token x the shared rate-limit budget
         (invariants 8 & 9). A denial here returns ``{"ok": False, ...}`` — the
         handler is NOT called.
      4. Dispatch to the spec's handler with (app_key, params).

    Returns a result dict: ``{"ok": bool, "message": str, "action_id": str, ...}``.
    Never raises for an expected gate/handler failure.
    """
    from backend.agent.governance import authorize
    from backend.agent.types import OperationalLevel

    params = params or {}
    spec = get_spec(action_id)

    # (1) Fail-closed on unknown action — the A/B line: known actions only.
    if spec is None:
        return {
            "ok": False,
            "action_id": action_id,
            "message": f"unknown action '{action_id}' — not in the registry (denied)",
        }

    # (2) Declared-but-pending actions cannot fire.
    if not spec.executable:
        return {
            "ok": False,
            "action_id": action_id,
            "message": f"action '{action_id}' is declared but not executable (no handler)",
        }

    level = operational_level if isinstance(operational_level, OperationalLevel) else None
    if level is None:
        level = OperationalLevel.SUPERVISED

    # (3) Shared governance gate — the one accounting every caller routes through.
    outcome = authorize(
        action_id=spec.id,
        app_key=app_key,
        tier=spec.tier,
        operational_level=level,
        approval_token=approval_token,
        pre_approved=pre_approved,
    )
    if not outcome.allow:
        return {
            "ok": False,
            "action_id": action_id,
            "needs_approval": outcome.needs_approval,
            "message": outcome.reason,
        }

    # (4) Dispatch.
    assert spec.handler is not None  # guaranteed by (2)
    try:
        result = spec.handler(app_key, params)
    except Exception as exc:  # never leak an executor exception to the seam
        log.warning("invoke_action: handler raised for %s/%s: %s", action_id, app_key, exc)
        return {
            "ok": False,
            "action_id": action_id,
            "message": f"handler error: {exc}",
        }
    result.setdefault("action_id", action_id)
    return result


__all__ = [
    "ActionView",
    "all_spec_meta",
    "build_registry",
    "diagnosis_to_fix_type",
    "executable_action_ids",
    "get_spec",
    "invoke_action",
    "list_actions",
    "safe_fix_types",
    "tier_for",
]
