"""backend/agent/smart_probe.py — SMART prefail + SnapRAID parity probe.

Two GROUND probes:

  1. **smart_status** — queries the Scrutiny app's REST API to detect SMART
     disk prefail or failed states.  Alert-only; no remediation.

  2. **snapraid_parity** — queries the snapraid-ui app's REST API to detect
     stale parity (older than 7 days).  Alert-only; no remediation.

Both probes:
  - Skip silently (return None) when the target app is not installed (absent
    from manifests).
  - Return INDETERMINATE when the app IS installed but the probe cannot read
    its data (network error, unexpected JSON shape, non-200 response).
  - Never emit personal infra (no IPs beyond 127.0.0.1, no hostnames, no
    usernames).
  - No new outbound payloads.  No subprocess writes.  No docker exec.

PINNED: ``reconcile_smart() -> list[Finding]``
"""

from __future__ import annotations

import datetime
from typing import Any

from backend.agent.spine import Finding, Verdict
from backend.core.logging import get_logger
from backend.manifests.loader import load_all_manifests

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SCRUTINY_DEFAULT_PORT = 8080
_SNAPRAID_DEFAULT_PORT = 8080
_PARITY_STALE_DAYS = 7
_HTTP_TIMEOUT = 5


# ---------------------------------------------------------------------------
# Sub-probe 1: Scrutiny SMART status
# ---------------------------------------------------------------------------


def _probe_scrutiny_smart() -> Finding | None:
    """Detect SMART disk prefail/failed status via the Scrutiny API.

    Returns None when Scrutiny is not installed.
    Returns INDETERMINATE when Scrutiny is installed but unreachable or returns
    unexpected data.
    Returns DRIFT when any drive has a failed/prefail SMART status.
    Returns VERIFIED when all drives pass.
    """
    manifests = load_all_manifests()
    app_manifest = manifests.get("scrutiny")
    if app_manifest is None:
        return None

    port = getattr(app_manifest, "web_port", None) or _SCRUTINY_DEFAULT_PORT
    base_url = f"http://127.0.0.1:{port}"
    physics = f"Scrutiny API GET {base_url}/api/summary"
    finding_id = "data.smart_status"

    try:
        import requests as _requests

        resp = _requests.get(f"{base_url}/api/summary", timeout=_HTTP_TIMEOUT)
    except Exception as exc:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="SMART probe: Scrutiny API unreachable",
            detail=f"{type(exc).__name__}: {exc}",
        )

    if resp.status_code != 200:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary=f"SMART probe: Scrutiny API returned HTTP {resp.status_code}",
            detail=f"status_code={resp.status_code}",
        )

    try:
        data: Any = resp.json()
    except Exception as exc:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="SMART probe: Scrutiny API response is not valid JSON",
            detail=f"{type(exc).__name__}: {exc}",
        )

    # Scrutiny /api/summary returns:
    #   {"data": {"summary": {<wwn>: {"device": {...}, "smart": {"status": 0|1}}}}}
    # status 0 = passed, non-zero = failed/prefail
    summary_block = None
    try:
        summary_block = data.get("data", {}).get("summary")
    except AttributeError:
        pass

    if not isinstance(summary_block, dict):
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="SMART probe: unexpected Scrutiny API response shape",
            detail=f"expected data.summary dict, got: {type(summary_block).__name__}",
        )

    failed: list[str] = []
    for wwn, entry in summary_block.items():
        try:
            smart_status = entry.get("smart", {}).get("status", 0)
            if smart_status != 0:
                device_name = entry.get("device", {}).get("name", wwn)
                failed.append(f"{device_name}(status={smart_status})")
        except AttributeError:
            failed.append(f"{wwn}(unreadable)")

    if failed:
        n = len(failed)
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.DRIFT,
            summary=f"SMART prefail detected: {n} drive(s) in failed/prefail state",
            detail=f"drives={', '.join(failed)}",
        )

    return Finding(
        id=finding_id,
        physics=physics,
        verdict=Verdict.VERIFIED,
        summary="SMART status: all drives passed",
        detail=f"checked={len(summary_block)} drive(s)",
    )


# ---------------------------------------------------------------------------
# Sub-probe 2: SnapRAID parity staleness
# ---------------------------------------------------------------------------


def _extract_last_sync_str(data: Any) -> str | None:
    """Pull a last-sync timestamp string from a snapraid-ui API payload.

    Tries several known response shapes; returns None when none are present.
    """
    if not isinstance(data, dict):
        return None
    try:
        return (
            data.get("last_sync")
            or data.get("lastSync")
            or (data.get("sync") or {}).get("date")
            or (data.get("status") or {}).get("last_sync")
        )
    except AttributeError:
        return None


def _parse_last_sync_dt(last_sync_str: str) -> datetime.datetime | None:
    """Parse a last-sync timestamp string into a naive datetime, or None."""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(str(last_sync_str), fmt)
        except ValueError:
            continue
    # Try fromisoformat as a catch-all (Python 3.7+)
    try:
        dt = datetime.datetime.fromisoformat(str(last_sync_str).replace("Z", "+00:00"))
        return dt.replace(tzinfo=None)
    except ValueError:
        return None


def _probe_snapraid_parity() -> Finding | None:
    """Detect stale SnapRAID parity via the snapraid-ui API.

    Returns None when snapraid-ui is not installed.
    Returns INDETERMINATE when installed but API is unreachable or returns
    unexpected data.
    Returns DRIFT when parity is older than 7 days.
    Returns VERIFIED when parity is within 7 days.
    """
    manifests = load_all_manifests()
    # Accept both key forms
    app_manifest = manifests.get("snapraid-ui") or manifests.get("snapraid_ui")
    if app_manifest is None:
        return None

    port = getattr(app_manifest, "web_port", None) or _SNAPRAID_DEFAULT_PORT
    base_url = f"http://127.0.0.1:{port}"
    physics = f"snapraid-ui API GET {base_url}/api/status"
    finding_id = "data.snapraid_parity"

    try:
        import requests as _requests

        resp = _requests.get(f"{base_url}/api/status", timeout=_HTTP_TIMEOUT)
    except Exception as exc:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="SnapRAID parity probe: snapraid-ui API unreachable",
            detail=f"{type(exc).__name__}: {exc}",
        )

    if resp.status_code != 200:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary=f"SnapRAID parity probe: snapraid-ui API returned HTTP {resp.status_code}",
            detail=f"status_code={resp.status_code}",
        )

    try:
        data: Any = resp.json()
    except Exception as exc:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="SnapRAID parity probe: snapraid-ui API response is not valid JSON",
            detail=f"{type(exc).__name__}: {exc}",
        )

    # snapraid-ui /api/status may return a dict with a "last_sync" or
    # "lastSync" or "sync" -> {"date": ...} field.  Try several known shapes.
    last_sync_str = _extract_last_sync_str(data)

    if not last_sync_str:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="SnapRAID parity probe: no last_sync timestamp in API response",
            detail=f"response_keys={list(data.keys()) if isinstance(data, dict) else type(data).__name__}",
        )

    # Parse the timestamp — try ISO 8601 and common formats
    last_sync_dt = _parse_last_sync_dt(last_sync_str)
    if last_sync_dt is None:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary="SnapRAID parity probe: cannot parse last_sync timestamp",
            detail=f"last_sync={last_sync_str!r}",
        )

    now = datetime.datetime.now(datetime.UTC).replace(tzinfo=None)
    age_days = (now - last_sync_dt).days

    if age_days > _PARITY_STALE_DAYS:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.DRIFT,
            summary=f"SnapRAID parity stale: last sync {age_days} days ago (threshold {_PARITY_STALE_DAYS}d)",
            detail=f"last_sync={last_sync_dt.isoformat()} age_days={age_days}",
        )

    return Finding(
        id=finding_id,
        physics=physics,
        verdict=Verdict.VERIFIED,
        summary=f"SnapRAID parity current: last sync {age_days} days ago",
        detail=f"last_sync={last_sync_dt.isoformat()} age_days={age_days}",
    )


# ---------------------------------------------------------------------------
# Public reconciler
# ---------------------------------------------------------------------------


def reconcile_smart() -> list[Finding]:
    """SMART / SnapRAID parity reconciler.

    Calls both sub-probes and returns all non-None results.  Each probe is
    independently guarded so one failure does not suppress the other.
    """
    findings: list[Finding] = []

    # Sub-probe 1: Scrutiny SMART status
    try:
        f = _probe_scrutiny_smart()
        if f is not None:
            findings.append(f)
    except Exception as exc:
        log.warning("scrutiny SMART probe raised unexpectedly: %s", exc)
        findings.append(
            Finding(
                id="data.smart_status",
                physics="Scrutiny API /api/summary",
                verdict=Verdict.INDETERMINATE,
                summary="SMART probe raised unexpected exception",
                detail=f"{type(exc).__name__}: {exc}",
            )
        )

    # Sub-probe 2: SnapRAID parity
    try:
        f = _probe_snapraid_parity()
        if f is not None:
            findings.append(f)
    except Exception as exc:
        log.warning("snapraid parity probe raised unexpectedly: %s", exc)
        findings.append(
            Finding(
                id="data.snapraid_parity",
                physics="snapraid-ui API /api/status",
                verdict=Verdict.INDETERMINATE,
                summary="SnapRAID parity probe raised unexpected exception",
                detail=f"{type(exc).__name__}: {exc}",
            )
        )

    return findings


__all__ = ["reconcile_smart"]
