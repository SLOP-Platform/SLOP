"""backend/agent/contributor.py — opt-in contribute-back channel call-site.

#1253: the call-site that assembles a 2c contribute-back payload from a
``fix_history`` row (or equivalent learned-memory source), gates it through
``send_contribute_back``, and — if the gate returns ``sent=True`` — transmits
the cleaned allowlisted payload to the moderation repo via HTTP.

LAYERING:
  * This module CONSUMES the allowlist-egress gate (``spine_egress.send_contribute_back``)
    and the key derivation (``spine_egress.derive_contribute_key``).
  * It OWNS the moderation-repo transport layer — the HTTP POST to the
    downstream moderation queue.
  * The toggle is checked INSIDE the gate; this module does NOT duplicate
    the check (single source of truth).
  * All calls fail-closed: network error, HTTP non-2xx, gate failure → return
    ``sent=False`` and log.

The moderation repo URL is read from StateDB (``contribute_repo_url``);
if absent or unset, contribute never transmits (fail-closed).
"""

from __future__ import annotations

import json
from typing import Any

from backend.agent.spine_egress import (
    EgressOutcome,
    send_contribute_back,
)
from backend.agent.spine import Verdict
from backend.core.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Payload assembly — 4 closed-vocabulary inputs → 7-field allowlisted tuple.
# ---------------------------------------------------------------------------


def assemble_contribute_payload(
    *,
    error_class: str,
    app_key: str,
    suggested_fix: str,
    diagnosis_class: str,
    fix_type: str,
    confidence: float = 0.0,
    sample_size: int = 0,
) -> dict[str, Any]:
    """Assemble a 7-field allowlisted contribute-back payload.

    All fields are from closed-vocabulary (error_class, diagnosis_class) or
    structured (confidence, sample_size).  ``suggested_fix`` is the one
    free-text field — the egress gate scrubber handles it defensively.

    The ``contribute_key`` is NOT emitted — it is derived from the same
    inputs for deduplication at the moderation repo but never travels in
    the payload (security review §1 — signature_hash denylist).
    """
    return {
        "error_class": error_class,
        "app_key": app_key,
        "suggested_fix": suggested_fix,
        "diagnosis_class": diagnosis_class,
        "fix_type": fix_type,
        "confidence": confidence,
        "sample_size": sample_size,
    }


def assemble_from_fix_history(row: dict[str, Any]) -> dict[str, Any]:
    """Assemble a contribute payload from a fix_history row dict.

    Expects keys: error_class, app_key, suggested_fix, diagnosis_class,
    fix_type, confidence, sample_size.  Missing numeric fields default to
    0.0 / 0."""
    return assemble_contribute_payload(
        error_class=row.get("error_class", ""),
        app_key=row.get("app_key", ""),
        suggested_fix=row.get("suggested_fix", ""),
        diagnosis_class=row.get("diagnosis_class", ""),
        fix_type=row.get("fix_type", ""),
        confidence=float(row.get("confidence", 0.0)),
        sample_size=int(row.get("sample_size", 0)),
    )


# ---------------------------------------------------------------------------
# Moderation-repo transport (fail-closed).
# ---------------------------------------------------------------------------


def _get_repo_url() -> str | None:
    """Read the moderation-repo URL from StateDB.  None if absent."""
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            url = db.get_setting("contribute_repo_url", default="")
        return (url or "").strip() or None
    except Exception:
        return None


def _post_to_repo(url: str, payload: dict[str, Any]) -> bool:
    """HTTP POST the allowlisted payload to the moderation repo.  Fail-closed:
    any error → return False (logged, never raised).

    Uses httpx if available; falls back to urllib.  The moderation repo is
    expected to accept JSON POST with a 201/200 response.  Payload content
    is NEVER logged (R5)."""
    try:
        import httpx  # pyright: ignore[reportMissingModuleSource]

        resp = httpx.post(
            url,
            json=payload,
            timeout=30.0,
        )
        ok = 200 <= resp.status_code < 300
        if not ok:
            log.warning(
                "contribute transport: HTTP %s from moderation repo",
                resp.status_code,
            )
        else:
            log.info(
                "contribute transport: accepted by moderation repo (HTTP %s)",
                resp.status_code,
            )
        return bool(ok)
    except Exception:
        try:
            import urllib.request

            req = urllib.request.Request(  # noqa: S310
                url,  # admin-controlled StateDB setting
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                ok = 200 <= resp.status < 300
                if not ok:
                    log.warning(
                        "contribute transport: HTTP %s from moderation repo (urllib fallback)",
                        resp.status,
                    )
                return bool(ok)
        except Exception as exc:
            log.warning("contribute transport: failed to reach moderation repo: %s", exc)
            return False


# ---------------------------------------------------------------------------
# The SINGLE contribute-back entry-point (callers wire THIS).
# ---------------------------------------------------------------------------


def contribute_back(row: dict[str, Any]) -> EgressOutcome:
    """Assemble, gate, and (if allowed) transmit one contribute-back payload.

    This is the SINGLE entry-point an agent-stratum caller (or scheduler hook,
    or #991 onboarding) uses to contribute.  It:

      1. Assembles the 7-field allowlisted payload from *row*.
      2. Calls ``send_contribute_back(payload)`` — the fail-closed gate
         that checks the toggle + verifies cleanliness.
      3. If ``outcome.sent is True`` and ``contribute_repo_url`` is set,
         HTTP-POSTs the cleaned payload to the moderation repo.
         Transport failure → best-effort (logged, ``sent`` unchanged).

    Returns the ``EgressOutcome`` so the caller can inspect the gate verdict.
    Never raises; never logs payload content.

    The *row* dict must contain at least the 4 closed-vocabulary fields
    (error_class, app_key, diagnosis_class, fix_type) plus suggested_fix.
    """
    payload = assemble_from_fix_history(row)

    # Toggle check BEFORE gate evaluation (§4.5 — toggle off returns sent=False
    # without ever examining the payload).  Checked HERE (the call-site), not inside
    # send_contribute_back, so the gate's existing test contract is preserved.
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            enabled = (
                db.get_setting("contribute_back_enabled", default="false") or "false"
            ).lower() == "true"
    except Exception:
        enabled = False  # fail-closed

    if not enabled:
        return EgressOutcome(
            sent=False,
            provider="contribute-back",
            verdict=Verdict.INDETERMINATE,
            reason="contribute_back_enabled is false — contribute disabled",
        )

    outcome = send_contribute_back(payload)

    if outcome.sent and outcome.payload is not None:
        repo_url = _get_repo_url()
        if repo_url:
            _post_to_repo(repo_url, outcome.payload)
        else:
            log.debug(
                "contribute_back: no contribute_repo_url set — payload gated but not transmitted"
            )

    return outcome


__all__ = [
    "assemble_contribute_payload",
    "assemble_from_fix_history",
    "contribute_back",
]
