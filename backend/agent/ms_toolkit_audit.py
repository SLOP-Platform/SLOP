"""backend/agent/ms_toolkit_audit.py — GROUND reconciler over the platform
self-check toolkit (``ms-check``).

The platform ships an operator-facing health-check script that runs ~38 checks
across software deps, file locations, the service, Docker, ports, ``.env``,
TLS, ghost resources, coverage, dependency CVEs, and git sync — and applies a
handful of auto-fixes.  Until now that signal was **invisible to the agent**:
the only machine-readable surface the spine consumed was ``ms-coverage``.

This reconciler runs ``ms-check --json`` (a read-only, structured mode that
emits no decoration and applies no privileged auto-fixes when unprivileged) and
turns each check into a frozen-verdict :class:`Finding`, exactly like
``self_audit.reconcile()``.  It reuses the existing toolkit signal rather than
re-implementing any of those 38 probes in Python — re-deriving an existing
signal is a defect.

Status → Verdict mapping (GROUND, against physics only):

  * ``pass`` → VERIFIED      — the check observed a healthy state.
  * ``fail`` → DRIFT         — the check observed something broken.
  * ``warn`` → INCONSISTENT  — a non-blocking disagreement (degraded), not a
    hard break; mirrors the script's own exit-code-1 (warnings) semantics.
  * ``info`` → (dropped)     — informational, not a ground verdict; never a
    Finding so it cannot mask a green/red signal.

If ``ms-check`` is unreachable, times out, exits without parseable JSON, or
emits no checks, the reconciler returns a single **INDETERMINATE** finding
(loud) — never a silent VERIFIED.  This module reads no docs (two-owner
firewall): it shells to a physics probe of the live host and parses its output.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

from backend.agent.spine import Finding, Verdict
from backend.core.logging import get_logger

log = get_logger(__name__)

REPO: Path = Path(__file__).resolve().parents[2]
_CHECK_SCRIPT: Path = REPO / "ms-check"
_TIMEOUT_S: float = 120.0

# Toolkit status string -> spine Verdict.  ``info`` deliberately has no entry —
# informational lines are not ground verdicts and are dropped before emission.
_STATUS_TO_VERDICT: dict[str, Verdict] = {
    "pass": Verdict.VERIFIED,
    "fail": Verdict.DRIFT,
    "warn": Verdict.INCONSISTENT,
}

_PHYSICS = "ms-check --json (platform self-check toolkit, read-only)"


def _indeterminate(summary: str, detail: str = "") -> list[Finding]:
    """Single loud INDETERMINATE finding when the toolkit signal is unreachable."""
    return [
        Finding(
            id="ms_toolkit.cycle",
            physics=_PHYSICS,
            verdict=Verdict.INDETERMINATE,
            summary=summary,
            detail=detail,
        )
    ]


def reconcile() -> list[Finding]:
    """Run ``ms-check --json`` and emit one Finding per non-informational check.

    Never raises.  An unreachable/unparseable toolkit yields one INDETERMINATE
    finding (loud), never a silent pass.  Each emitted Finding carries the
    toolkit's own stable check id (``<section>.<n>``) so a verdict is traceable
    back to the exact probe that produced it.
    """
    if not _CHECK_SCRIPT.exists():
        return _indeterminate(
            "platform self-check toolkit not found",
            detail=f"ms-check absent at {_CHECK_SCRIPT}",
        )

    try:
        proc = subprocess.run(
            ["bash", str(_CHECK_SCRIPT), "--json"],
            cwd=str(REPO),
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return _indeterminate(
            "platform self-check timed out",
            detail=f"ms-check did not return within {_TIMEOUT_S:.0f}s",
        )
    except Exception as exc:  # unreachable ground source is loud
        log.warning("ms_toolkit_audit: ms-check invocation failed: %s", exc)
        return _indeterminate(
            "platform self-check unreachable",
            detail=f"ms-check raised: {type(exc).__name__}",
        )

    # ms-check exits 0/1/2 by design (clean/warnings/errors).  A non-zero code
    # is NOT a failure of the probe — the checks themselves are the signal — so
    # we parse stdout regardless of returncode.  Only an unparseable payload is
    # INDETERMINATE.
    raw = (proc.stdout or "").strip()
    if not raw:
        return _indeterminate(
            "platform self-check produced no output",
            detail=f"ms-check exit {proc.returncode}; stderr: {(proc.stderr or '').strip()[:200]}",
        )

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _indeterminate(
            "platform self-check output is not valid JSON",
            detail=f"parse error: {exc}",
        )

    checks = data.get("checks") if isinstance(data, dict) else None
    if not isinstance(checks, list) or not checks:
        return _indeterminate(
            "platform self-check emitted no checks",
            detail=f"summary={data.get('summary') if isinstance(data, dict) else 'n/a'}",
        )

    findings: list[Finding] = []
    for check in checks:
        if not isinstance(check, dict):
            continue
        status = str(check.get("status", "")).lower()
        verdict = _STATUS_TO_VERDICT.get(status)
        if verdict is None:
            # info (or any unrecognised status) is not a ground verdict — drop.
            continue
        check_id = str(check.get("id") or "ms_toolkit.unknown")
        section = str(check.get("section") or "")
        detail = str(check.get("detail") or "")
        findings.append(
            Finding(
                id=f"ms_toolkit.{check_id}",
                physics=_PHYSICS,
                verdict=verdict,
                summary=f"[{section or check_id}] {status}",
                detail=detail,
            )
        )

    if not findings:
        # All checks were informational — the toolkit ran but asserted nothing
        # falsifiable.  Loud rather than a silent green.
        return _indeterminate(
            "platform self-check returned only informational results",
            detail=f"{len(checks)} info-only check(s); no pass/warn/fail verdicts",
        )

    return findings


__all__ = ["reconcile"]
