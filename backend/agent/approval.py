"""backend/agent/approval.py — authz-bound, replay-proof, single-use approval tokens.

Realizes safety invariant 8 (operational plan §3): a conversational/MCP "do it"
binds to a SPECIFIC pending action via a nonce token that is:

  * **authz-bound**  — issued for one (action_id, app_key) pair; presenting it for
    any other action/app fails.
  * **replay-proof / single-use** — consumed on first successful validation; a
    second use fails.
  * **short-TTL**    — expires after ``_TTL_S`` seconds.

A free-text "do it" that is NOT backed by a token issued here can therefore never
satisfy a T3 always-ask (the governance gate calls :func:`consume_approval_token`).

Storage is in-process and ephemeral by design: an approval is a short-lived
intent, not durable state. A process restart invalidates all outstanding
approvals — the safe direction (the user simply re-confirms).
"""

from __future__ import annotations

import secrets
import threading
import time
from dataclasses import dataclass

from backend.core.logging import get_logger

log = get_logger(__name__)

_TTL_S = 120  # an approval is valid for two minutes


@dataclass(frozen=True)
class _Approval:
    action_id: str
    app_key: str
    expires_at: float


_lock = threading.Lock()
_store: dict[str, _Approval] = {}


def issue_approval_token(action_id: str, app_key: str) -> str:
    """Mint a single-use approval token bound to (action_id, app_key).

    Returned to the operator at confirm time; presented back as the
    ``approval_token`` on the subsequent ``invoke_action`` call.
    """
    token = secrets.token_urlsafe(32)
    with _lock:
        _store[token] = _Approval(
            action_id=action_id,
            app_key=app_key,
            expires_at=time.time() + _TTL_S,
        )
    log.info("approval: issued token for action=%s app=%s", action_id, app_key)
    return token


def consume_approval_token(token: str, *, action_id: str, app_key: str) -> bool:
    """Validate AND consume *token* for (action_id, app_key).

    Returns True exactly once for a token that is: present, unexpired, and bound
    to the SAME (action_id, app_key). The token is removed on any terminal
    outcome (success, expiry, or mismatch) so it can never be replayed. Fail-
    closed: anything unrecognised returns False.
    """
    now = time.time()
    with _lock:
        appr = _store.pop(token, None)  # single-use: remove on first touch
        if appr is None:
            return False
        if appr.expires_at < now:
            log.info("approval: token expired for action=%s app=%s", action_id, app_key)
            return False
        if appr.action_id != action_id or appr.app_key != app_key:
            # Authz-bound: re-binding to a different action/app is a hard fail.
            log.warning(
                "approval: token mismatch (bound to %s/%s, presented for %s/%s)",
                appr.action_id,
                appr.app_key,
                action_id,
                app_key,
            )
            return False
    return True


def _reset_for_tests() -> None:
    """Test-only: clear all outstanding approvals."""
    with _lock:
        _store.clear()


__all__ = [
    "consume_approval_token",
    "issue_approval_token",
]
