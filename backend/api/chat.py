"""backend/api/chat.py — N6 operator chat slice (operational plan §W6-min).

ONE conversational control surface: text → intent → registry action → compose. It
is a thin front-end over the PINNED tool-surface seam — it NEVER reimplements
dispatch. Every action flows through ``backend.agent.registry.invoke_action``,
which routes through the shared governance gate (invariants 8 & 9). Chat is just a
client of that seam, exactly like the future MCP adapter (N9).

Two intent shapes:

  * **read-only** (status / list-actions / what-can-you-do) — answered immediately,
    no mutation, no token. Bound to READ scope.
  * **action** (restart/repull/restart-service for an app) — bound to ACT scope and
    the N5 tierxscope pre-approval policy:
      - tier pre-approved for the app  ⇒ act-then-report (the autonomous path).
      - NOT pre-approved               ⇒ ask: issue a SINGLE-USE approval token
        bound to (action_id, app_key); the operator's typed "do it" + that token is
        the per-action approval. (Safety invariant 8: a free-text "do it" with NO
        token never satisfies anything; a token NEVER satisfies a T3 always-ask —
        the governance gate enforces both.)

Fail-closed (safety invariant 2): an unrecognised intent ⇒ ASK for clarification,
never guess an action. No invented actions (the A/B line) — intents resolve only to
ids already in the registry.
"""

from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from backend.agent.registry import invoke_action, list_actions, tier_for
from backend.api.auth import Scope, require_scope
from backend.core.logging import get_logger

log = get_logger(__name__)
router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response models.
# ---------------------------------------------------------------------------


class ChatRequest(BaseModel):
    message: str
    # Optional explicit context. The operator may pass an app_key directly (e.g.
    # the chat panel knows which app card it was opened from) rather than relying
    # on NL extraction.
    app_key: str | None = None
    # A previously-issued single-use approval token, presented to confirm a
    # not-pre-approved action ("do it"). Bound to (action_id, app_key).
    approval_token: str | None = None


class ChatReply(BaseModel):
    reply: str
    # "answer" (read-only done) | "acted" (action ran) | "needs_approval" (ask) |
    # "denied" (gate refused) | "clarify" (unrecognised intent).
    kind: str
    action_id: str | None = None
    app_key: str | None = None
    # Present only on a needs_approval reply: a single-use token the client echoes
    # back as approval_token to confirm.
    approval_token: str | None = None


# ---------------------------------------------------------------------------
# Deterministic intent classification (fail-closed, no invented actions).
# ---------------------------------------------------------------------------


# Map an intent verb to a registry action id. Keys are matched as whole words.
_ACTION_VERBS: dict[str, str] = {
    "restart": "restart_container",
    "reboot": "restart_container",
    "bounce": "restart_container",
    "repull": "repull_restart",
    "re-pull": "repull_restart",
    "update": "repull_restart",
    "pull": "repull_restart",
}

_STATUS_WORDS = ("status", "health", "how is", "how's", "what's wrong", "whats wrong", "report")
_LIST_WORDS = ("what can you", "list actions", "what actions", "capabilities", "help")


def _extract_app_key(message: str, explicit: str | None) -> str | None:
    """Resolve an app_key: an explicit one wins; else match a known app name in the
    message. Returns None when no known app is referenced (fail-closed — no guess)."""
    if explicit:
        return explicit.strip()
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            keys = [a.key for a in db.get_all_apps()]
    except Exception as exc:
        log.debug("chat: could not list apps for app-key extraction: %s", exc)
        return None
    lowered = message.lower()
    for key in keys:
        if re.search(rf"\b{re.escape(key.lower())}\b", lowered):
            return key
    return None


def classify_intent(message: str, explicit_app: str | None) -> dict[str, Any]:
    """Classify *message* into a structured intent. Fail-closed: anything not
    clearly a known read-only or action intent ⇒ ``clarify``.

    Returns {"kind": "status"|"list"|"action"|"clarify", "action_id"?, "app_key"?}.
    """
    text = message.strip().lower()
    if not text:
        return {"kind": "clarify"}

    if any(w in text for w in _LIST_WORDS):
        return {"kind": "list"}

    # Action intents: a known verb + a resolvable app.
    for verb, action_id in _ACTION_VERBS.items():
        if re.search(rf"\b{re.escape(verb)}\b", text):
            app_key = _extract_app_key(message, explicit_app)
            if app_key is None:
                return {"kind": "clarify", "reason": "which app?", "action_id": action_id}
            return {"kind": "action", "action_id": action_id, "app_key": app_key}

    if any(w in text for w in _STATUS_WORDS):
        return {"kind": "status", "app_key": _extract_app_key(message, explicit_app)}

    return {"kind": "clarify"}


# ---------------------------------------------------------------------------
# Read-only intent handlers.
# ---------------------------------------------------------------------------


def _status_reply(app_key: str | None) -> ChatReply:
    """Compose a read-only status answer from the latest health snapshot."""
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            apps = db.get_all_apps()
        if app_key:
            match = [a for a in apps if a.key == app_key]
            if not match:
                return ChatReply(reply=f"I don't manage an app called '{app_key}'.", kind="answer")
            a = match[0]
            return ChatReply(
                reply=f"{a.key}: status={getattr(a, 'status', 'unknown')}.",
                kind="answer",
                app_key=app_key,
            )
        names = ", ".join(a.key for a in apps) or "(none installed)"
        return ChatReply(reply=f"I'm managing: {names}. Ask about a specific app for detail.", kind="answer")
    except Exception as exc:
        log.warning("chat: status reply failed: %s", exc)
        return ChatReply(reply="I couldn't read the current status right now.", kind="answer")


def _list_actions_reply() -> ChatReply:
    """List the registry action vocabulary (read-only projection)."""
    views = list_actions()
    lines = [
        f"- {v.id} (T{v.tier}{'' if v.executable else ', not yet executable'}): {v.description}"
        for v in views
    ]
    return ChatReply(reply="I can take these actions:\n" + "\n".join(lines), kind="answer")


# ---------------------------------------------------------------------------
# The endpoint.
# ---------------------------------------------------------------------------


@router.post("", response_model=ChatReply)
def chat(req: ChatRequest, _scope: Scope = Depends(require_scope(Scope.ACT))) -> ChatReply:
    """Operator chat. Bound to ACT scope (it can dispatch actions); read-only
    intents simply never reach a mutation. Action dispatch goes ONLY through
    ``registry.invoke_action`` (the shared gate) — never a direct handler call.
    """
    intent = classify_intent(req.message, req.app_key)
    kind = intent["kind"]

    if kind == "list":
        return _list_actions_reply()
    if kind == "status":
        return _status_reply(intent.get("app_key"))
    if kind == "clarify":
        if intent.get("reason") == "which app?":
            return ChatReply(
                reply="Which app should I act on? Name it (e.g. 'restart sonarr').",
                kind="clarify",
                action_id=intent.get("action_id"),
            )
        return ChatReply(
            reply="I'm not sure what you want. I can report status or restart/repull a specific app.",
            kind="clarify",
        )

    # kind == "action"
    action_id = intent["action_id"]
    app_key = intent["app_key"]
    return _dispatch_action(action_id, app_key, req.approval_token)


def _dispatch_action(action_id: str, app_key: str, approval_token: str | None) -> ChatReply:
    """Resolve the N5 policy, then call invoke_action through the shared gate.

    - tier pre-approved for the app ⇒ pre_approved=True ⇒ act-then-report.
    - else, if a token is presented ⇒ pass it (the gate validates single-use,
      authz-bound, and refuses it for a T3).
    - else ⇒ ask: issue a single-use token bound to (action_id, app_key).
    """
    from backend.agent.policy import load_policy

    tier = tier_for(action_id)
    policy = load_policy()
    pre_approved = policy.is_pre_approved(tier, app_key)

    # Not pre-approved AND no token yet ⇒ ask, minting a single-use bound token.
    # (A T3 is never pre_approved; it always lands here and ALWAYS requires the
    # token — and even the token cannot satisfy a T3 in the gate, so a T3 stays
    # always-ask through a human approval surface, never free-text.)
    if not pre_approved and not approval_token:
        from backend.agent.approval import issue_approval_token

        token = issue_approval_token(action_id, app_key)
        return ChatReply(
            reply=(
                f"'{action_id}' on {app_key} is tier T{int(tier)} and not pre-approved. "
                "Confirm to proceed (reply 'do it')."
            ),
            kind="needs_approval",
            action_id=action_id,
            app_key=app_key,
            approval_token=token,
        )

    result = invoke_action(
        action_id,
        params={},
        approval_token=approval_token,
        app_key=app_key,
        operational_level=_operational_level(),
        pre_approved=pre_approved,
    )

    if result.get("ok"):
        return ChatReply(
            reply=f"Done — {result.get('message', action_id + ' completed')}.",
            kind="acted",
            action_id=action_id,
            app_key=app_key,
        )
    if result.get("needs_approval"):
        # The gate refused for lack of a valid token (e.g. a T3, or a consumed/
        # expired token). Re-ask, but do NOT silently auto-mint for a T3 — the gate
        # already says a token is required; surface its reason verbatim.
        return ChatReply(
            reply=f"I can't do that without explicit approval: {result.get('message', '')}",
            kind="needs_approval",
            action_id=action_id,
            app_key=app_key,
        )
    return ChatReply(
        reply=f"I couldn't do that: {result.get('message', 'denied')}",
        kind="denied",
        action_id=action_id,
        app_key=app_key,
    )


def _operational_level() -> Any:
    """Read the configured operational level (defaults to SUPERVISED)."""
    from backend.agent.types import OperationalLevel

    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            raw = db.get_setting("agent_operational_level")
        return OperationalLevel.from_setting(raw)
    except Exception:
        return OperationalLevel.SUPERVISED


__all__ = ["classify_intent", "router"]
