"""backend/agent/mcp_adapter.py — the MCP product tool-surface seam (N9, #1074).

A **thin, transport-agnostic adapter** that exposes the Action Registry over the
Model Context Protocol. It is the MCP-side sibling of ``backend/api/chat.py`` (the
N6 chat slice): like chat, it is just another client of the PINNED tool-surface
seam (``backend.agent.registry.list_actions`` / ``invoke_action``) and NEVER
reimplements dispatch — every mutation routes through ``invoke_action`` and so
through the shared governance gate (invariants 8 & 9).

WHAT THIS MODULE IS (and is NOT):
  * IS — the pure mapping ``MCP tool call → the pinned seam``: it derives the MCP
    ``tools/list`` definitions from the registry and routes a ``tools/call`` to
    ``list_actions`` / ``invoke_action``, wrapping the result in the MCP content
    envelope. Fully unit-testable with no network.
  * IS NOT — a live MCP server/transport (stdio or streamable-HTTP). Mounting a
    network-exposed agent-action surface is a distinct, security-reviewed,
    operator-gated follow-on (it needs auth wiring + a dependency decision); this
    slice deliberately ships only the safe, verifiable seam. The transport layer,
    when added, calls :func:`tool_definitions` for ``tools/list`` and
    :func:`dispatch` for ``tools/call`` — nothing more.

TWO META-TOOLS (not one-per-action) — by design:
  The registry pins exactly two seam functions; this adapter exposes exactly two
  MCP tools mirroring them, rather than one tool per action. That keeps a SINGLE
  gated invocation chokepoint on the MCP side — the direct expression of invariant
  9 ("every registry-handler invocation routes through one chokepoint") — and
  avoids per-action ``inputSchema`` drift. A client lists actions, then invokes by
  id, exactly as chat resolves a read intent then an action intent.

    * ``list_agent_actions``  — read-only discovery (no args, no mutation, no
      token). Returns the FULL declared vocabulary with tiers, mirroring
      ``list_actions``. Bound conceptually to READ scope.
    * ``invoke_agent_action`` — the single gated invoke. Routes to
      ``invoke_action`` → the shared governance gate, which fail-closes on tier x
      policy x approval-token x rate-limit. Bound conceptually to ACT scope.

Fail-closed everywhere: an unknown tool name, a missing required argument, or a
non-executable / unknown action all return an ``isError`` envelope — never a raise,
never a silent success, never a handler dispatched outside the gate.
"""

from __future__ import annotations

import json
from typing import Any

from backend.agent.registry import (
    executable_action_ids,
    invoke_action,
    list_actions,
)
from backend.core.logging import get_logger

log = get_logger(__name__)

# MCP tool names — stable wire identifiers a transport advertises in tools/list.
TOOL_LIST = "list_agent_actions"
TOOL_INVOKE = "invoke_agent_action"


# ---------------------------------------------------------------------------
# MCP content envelope helpers (the tools/call result shape).
# ---------------------------------------------------------------------------


def _envelope(payload: dict[str, Any], *, is_error: bool) -> dict[str, Any]:
    """Wrap *payload* in the MCP ``tools/call`` result shape.

    MCP returns a list of content blocks plus an ``isError`` flag; we serialize the
    structured payload as one JSON text block (machine- and LLM-readable) and also
    surface it as ``structuredContent`` for transports/clients that consume it
    directly. Never raises — serialization of our own dicts is total.
    """
    return {
        "content": [{"type": "text", "text": json.dumps(payload, sort_keys=True)}],
        "structuredContent": payload,
        "isError": is_error,
    }


def _error(message: str) -> dict[str, Any]:
    return _envelope({"ok": False, "message": message}, is_error=True)


# ---------------------------------------------------------------------------
# tools/list — definitions derived from the registry.
# ---------------------------------------------------------------------------


def tool_definitions() -> list[dict[str, Any]]:
    """Return the MCP tool definitions for ``tools/list``, derived from the registry.

    The ``invoke_agent_action`` tool constrains ``action_id`` to the set of
    EXECUTABLE action ids (``executable_action_ids``) at the JSON-Schema level, so
    a client is never offered a declared-but-pending action it cannot run. The
    full vocabulary (including pending stubs) is still discoverable via
    ``list_agent_actions``. The enum is registry-derived, so the surface can never
    drift from the registry (N1).
    """
    invokable = sorted(executable_action_ids())
    return [
        {
            "name": TOOL_LIST,
            "description": (
                "List the SLOP agent's full action vocabulary (id, tier, "
                "reversibility, whether executable, and description). Read-only; "
                "takes no arguments and mutates nothing."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
        },
        {
            "name": TOOL_INVOKE,
            "description": (
                "Invoke one SLOP agent action by id, scoped to one app. Routed "
                "through the shared governance gate: tier x pre-approval policy x "
                "single-use approval_token x rate-limit are enforced fail-closed. "
                "An action requiring approval that is not pre-approved returns "
                "needs_approval=true rather than acting."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "action_id": {
                        "type": "string",
                        "description": "Registry action id to invoke.",
                        # Registry-derived: only actions that can actually run.
                        "enum": invokable,
                    },
                    "app_key": {
                        "type": "string",
                        "description": "The managed app the action targets.",
                    },
                    "params": {
                        "type": "object",
                        "description": "Optional action-specific parameters.",
                    },
                    "approval_token": {
                        "type": "string",
                        "description": (
                            "Single-use token bound to (action_id, app_key) "
                            "confirming a not-pre-approved action."
                        ),
                    },
                },
                "required": ["action_id", "app_key"],
                "additionalProperties": False,
            },
        },
    ]


# ---------------------------------------------------------------------------
# tools/call — dispatch to the pinned seam.
# ---------------------------------------------------------------------------


def _handle_list() -> dict[str, Any]:
    actions = [
        {
            "id": v.id,
            "tier": v.tier,
            "reversible": v.reversible,
            "executable": v.executable,
            "scopeable": v.scopeable,
            "default_rate_limit": v.default_rate_limit,
            "diagnosis_classes": list(v.diagnosis_classes),
            "description": v.description,
        }
        for v in list_actions()
    ]
    return _envelope({"ok": True, "actions": actions}, is_error=False)


def _handle_invoke(arguments: dict[str, Any]) -> dict[str, Any]:
    action_id = arguments.get("action_id")
    app_key = arguments.get("app_key")
    if not isinstance(action_id, str) or not action_id:
        return _error("invoke_agent_action: missing required 'action_id'")
    if not isinstance(app_key, str) or not app_key:
        return _error("invoke_agent_action: missing required 'app_key'")

    params = arguments.get("params")
    if params is not None and not isinstance(params, dict):
        return _error("invoke_agent_action: 'params' must be an object")

    approval_token = arguments.get("approval_token")
    if approval_token is not None and not isinstance(approval_token, str):
        return _error("invoke_agent_action: 'approval_token' must be a string")

    # The single gated chokepoint. We pass NO operational_level, so invoke_action
    # defaults to SUPERVISED (the most conservative level — requires the gate's
    # confirmation/approval), exactly mirroring chat.py. We never set
    # pre_approved here: the MCP caller's authority is the approval_token (or the
    # app's standing pre-approval policy, which the gate reads itself), never a
    # flag this adapter asserts.
    result = invoke_action(
        action_id,
        params or {},
        approval_token,
        app_key=app_key,
    )
    is_error = not bool(result.get("ok"))
    return _envelope(result, is_error=is_error)


def dispatch(name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Route an MCP ``tools/call`` to the pinned seam and return the result envelope.

    Fail-closed: an unknown tool name or a malformed argument set returns an
    ``isError`` envelope. Never raises for an expected client error; a defensive
    catch wraps any unexpected internal error so the transport gets an envelope,
    not an exception.
    """
    args = arguments or {}
    try:
        if name == TOOL_LIST:
            return _handle_list()
        if name == TOOL_INVOKE:
            return _handle_invoke(args)
        return _error(f"unknown MCP tool '{name}' (denied)")
    except Exception as exc:  # pragma: no cover - defensive; the seam is total
        log.warning("mcp_adapter.dispatch: unexpected error for %s: %s", name, exc)
        return _error(f"internal error handling '{name}'")


__all__ = [
    "TOOL_INVOKE",
    "TOOL_LIST",
    "dispatch",
    "tool_definitions",
]
