"""backend/agent/scrub_probe.py — Scrub-effectiveness probe.

Continuously validates that scrub() catches all its claimed categories.
If any canonical test payload passes through scrub() unchanged (i.e., no
placeholder substitution occurred), emits DRIFT. This creates a regression
gate: if a future change weakens a scrub pattern, the probe goes red.
"""

from __future__ import annotations

from backend.agent.spine import Finding, Verdict
from backend.agent.scrub import scrub
from backend.core.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Probe corpus — one test string per category, each guaranteed to trigger one
# or more scrub substitutions against the production scrub() rules.
# ---------------------------------------------------------------------------
_PROBE_CORPUS: dict[str, str] = {
    "ipv4": "Connected to 192.168.1.50 port 22",
    "hostname": "nas-prod-01.internal.example.com is unreachable",
    "email": "owner@internal.example.com sent a request",
    "bearer": "Authorization: Bearer sk-1234567890abcdef1234567890abcdef12",
    "path": "Config loaded from /opt/slop/config/settings.json",
    "hex_token": "session=a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2",
}


def check_scrub_effectiveness() -> Finding:
    """Check that scrub() catches all _PROBE_CORPUS categories.

    Returns:
        Finding with:
          - VERIFIED if all 6 categories are caught (scrub output != input)
          - DRIFT    if any category escapes scrub unchanged
          - INDETERMINATE if scrub() raises an unexpected exception
    """
    try:
        escaped = []
        for category, test_string in _PROBE_CORPUS.items():
            try:
                result = scrub(test_string)
                if result == test_string:
                    escaped.append(category)
            except Exception as exc:
                return Finding(
                    id="integrity.scrub_effectiveness",
                    physics="backend.agent.scrub.scrub() called with _PROBE_CORPUS",
                    verdict=Verdict.INDETERMINATE,
                    summary=f"scrub-effectiveness probe: scrub() raised {type(exc).__name__}",
                    detail="",
                )

        if escaped:
            n = len(escaped)
            return Finding(
                id="integrity.scrub_effectiveness",
                physics="backend.agent.scrub.scrub() called with _PROBE_CORPUS",
                verdict=Verdict.DRIFT,
                summary=f"scrub-effectiveness: {n}/6 patterns escaped",
                detail=f"escaped categories: {', '.join(escaped)}",
            )
        return Finding(
            id="integrity.scrub_effectiveness",
            physics="backend.agent.scrub.scrub() called with _PROBE_CORPUS",
            verdict=Verdict.VERIFIED,
            summary="scrub-effectiveness: all 6 patterns caught",
            detail="",
        )
    except Exception as exc:
        return Finding(
            id="integrity.scrub_effectiveness",
            physics="backend.agent.scrub.scrub() called with _PROBE_CORPUS",
            verdict=Verdict.INDETERMINATE,
            summary=f"scrub-effectiveness probe: unexpected error: {type(exc).__name__}",
            detail="",
        )


__all__ = ["check_scrub_effectiveness"]
