"""backend/agent/cve_audit.py — CVE remediation probe (GROUND + gated auto-heal).

A read-only GROUND probe that scans the images of managed apps and SLOP's own
image for HIGH/CRITICAL vulnerabilities using the bundled ``trivy`` scanner, then
emits a ``health.cve`` :class:`Finding` per image.

GROUND source: ``trivy image --format json --severity HIGH,CRITICAL <ref>``.
The scanner reads the local image layers — physics, not docs. When trivy is
absent, times out, or fails, the probe yields INDETERMINATE (loud) per image,
never a silent VERIFIED.

Verdict mapping:
  - HIGH/CRITICAL vulns present  → DRIFT  (summary names the counts)
  - no HIGH/CRITICAL vulns       → VERIFIED
  - trivy unavailable/error      → INDETERMINATE

Auto-heal (separate from the probe; never runs inside reconcile):
  When a DRIFT is observed and a newer image digest is available in the registry
  (REUSES the digest machinery in ``backend/api/updates.py``), the fix is to
  re-pull + restart the affected container. This REUSES the existing
  ``_heal_pull_image`` action in ``backend/health/checker.py`` — no new pull path.

  The action is GATED on both:
    1. The per-app auto-update preference (``notify_only`` off AND not ``pinned``
       in updates.py preferences), AND
    2. The agent :class:`OperationalLevel` — only ``AUTONOMOUS`` executes; any
       other level (SUPERVISED default, ADVISORY) merely PROPOSES.

  After a successful pull+restart, the image is re-scanned to confirm the CVEs
  cleared; the confirmation verdict is returned to the caller.

No personal infra in any output: image references and CVE ids only — no
hostnames, IPs, or usernames.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

from backend.agent.spine import Finding, Verdict
from backend.agent.types import OperationalLevel
from backend.core.logging import get_logger

log = get_logger(__name__)

# Severities the probe treats as actionable drift.
_DRIFT_SEVERITIES = ("HIGH", "CRITICAL")

# trivy can be slow on a cold cache; give it room but bound it.
_SCAN_TIMEOUT_S = 300

# SLOP's own image is scanned under this synthetic app key.
SELF_APP_KEY = "slop"


# ---------------------------------------------------------------------------
# trivy invocation + parsing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CveScanResult:
    """Parsed outcome of a single trivy scan.

    ``available`` is False when trivy could not run (missing/timeout/error) —
    the probe maps that to INDETERMINATE. When True, ``high`` and ``critical``
    carry the vulnerability counts and ``ids`` the (bounded) CVE id list.
    """

    available: bool
    high: int = 0
    critical: int = 0
    ids: tuple[str, ...] = ()
    error: str = ""

    @property
    def total(self) -> int:
        return self.high + self.critical


def _parse_trivy_json(raw: str) -> CveScanResult:
    """Parse trivy JSON output into a :class:`CveScanResult`.

    Counts HIGH and CRITICAL vulnerabilities across all results. Returns an
    unavailable result if the payload cannot be parsed (defensive — a malformed
    scan is INDETERMINATE, never a false VERIFIED).
    """
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        return CveScanResult(available=False, error=f"unparseable trivy json: {exc}")

    high = 0
    critical = 0
    ids: list[str] = []
    for result in data.get("Results", []) or []:
        for vuln in result.get("Vulnerabilities", []) or []:
            sev = str(vuln.get("Severity", "")).upper()
            if sev == "HIGH":
                high += 1
            elif sev == "CRITICAL":
                critical += 1
            else:
                continue
            vid = vuln.get("VulnerabilityID")
            if vid and vid not in ids:
                ids.append(vid)

    # Bound the id list so a Finding detail stays allowlist-safe and small.
    return CveScanResult(
        available=True,
        high=high,
        critical=critical,
        ids=tuple(ids[:20]),
    )


def scan_image(image_ref: str) -> CveScanResult:
    """Scan one image reference with trivy for HIGH/CRITICAL CVEs.

    Read-only: ``trivy image`` inspects local layers and never mutates the
    image. Returns an unavailable result (→ INDETERMINATE) when trivy is not
    installed, times out, or exits with an error.
    """
    cmd = [
        "trivy",
        "image",
        "--quiet",
        "--format",
        "json",
        "--severity",
        ",".join(_DRIFT_SEVERITIES),
        image_ref,
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_SCAN_TIMEOUT_S,
        )
    except FileNotFoundError:
        return CveScanResult(available=False, error="trivy not found")
    except subprocess.TimeoutExpired:
        return CveScanResult(available=False, error="trivy scan timed out")

    if result.returncode != 0:
        return CveScanResult(
            available=False,
            error=f"trivy exit {result.returncode}: {result.stderr.strip()[:160]}",
        )

    return _parse_trivy_json(result.stdout)


# ---------------------------------------------------------------------------
# Probe
# ---------------------------------------------------------------------------


def _image_ref_for(app: Any) -> str:
    """Build the image reference for an app manifest object."""
    image = getattr(app, "image", "") or ""
    tag = getattr(app, "image_tag", "") or "latest"
    if not image:
        return ""
    return f"{image}:{tag}"


def _probe_cve(app: Any, image_ref: str | None = None) -> Finding | None:
    """GROUND CVE probe for one app image.

    Returns ``None`` when the app declares no image (nothing to scan).
    """
    app_key = getattr(app, "app_key", None) or getattr(app, "key", None) or "unknown"
    ref = image_ref if image_ref is not None else _image_ref_for(app)
    if not ref:
        return None

    finding_id = f"health.cve.{app_key}"
    physics = f"trivy image --severity HIGH,CRITICAL {ref}"

    scan = scan_image(ref)
    if not scan.available:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.INDETERMINATE,
            summary=f"cve scan unavailable for {app_key}",
            detail=scan.error,
        )

    if scan.total > 0:
        return Finding(
            id=finding_id,
            physics=physics,
            verdict=Verdict.DRIFT,
            summary=(
                f"{app_key}: {scan.critical} critical / {scan.high} high "
                f"vulnerabilit{'y' if scan.total == 1 else 'ies'} in {ref}"
            ),
            detail=f"image={ref} critical={scan.critical} high={scan.high} ids={list(scan.ids)}",
        )

    return Finding(
        id=finding_id,
        physics=physics,
        verdict=Verdict.VERIFIED,
        summary=f"{app_key}: no HIGH/CRITICAL CVEs in {ref}",
        detail=f"image={ref}",
    )


def _self_image_ref() -> str:
    """Return SLOP's own running image reference, or '' if undeterminable.

    Best-effort via the docker SDK; missing/unreachable Docker yields '' which
    omits the self finding rather than emitting a misleading INDETERMINATE.
    """
    try:
        from backend.api.updates import SELF_CONTAINER_KEY
        from backend.core.docker_client import client

        dc = client()
        for c in dc.containers.list():
            if c.name == SELF_CONTAINER_KEY:
                return (c.attrs.get("Config") or {}).get("Image", "") or ""
    except Exception as exc:  # best-effort — absence is not a probe failure
        log.debug("could not resolve self image ref: %s", exc)
    return ""


def reconcile_cve(apps: list[Any]) -> list[Finding]:
    """GROUND CVE reconciler over managed app images + SLOP's own image.

    Each app's image is scanned independently; one failure yields its own
    INDETERMINATE without suppressing the others. SLOP's own image is scanned
    last under the synthetic key ``slop`` when its running ref can be resolved.
    """
    findings: list[Finding] = []

    for app in apps:
        app_key = getattr(app, "app_key", None) or getattr(app, "key", "unknown")
        try:
            f = _probe_cve(app)
            if f is not None:
                findings.append(f)
        except Exception as exc:
            log.warning("cve probe failed for %s: %s", app_key, exc)
            findings.append(
                Finding(
                    id=f"health.cve.{app_key}",
                    physics=f"trivy image scan for {app_key}",
                    verdict=Verdict.INDETERMINATE,
                    summary=f"cve probe raised unexpectedly for {app_key}",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

    # SLOP's own image — omit silently if the ref can't be resolved.
    self_ref = _self_image_ref()
    if self_ref:
        try:
            self_app = type("SelfApp", (), {"app_key": SELF_APP_KEY})()
            f = _probe_cve(self_app, image_ref=self_ref)
            if f is not None:
                findings.append(f)
        except Exception as exc:
            log.warning("cve self-probe failed: %s", exc)

    return findings


# ---------------------------------------------------------------------------
# Gated auto-heal
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CveHealDecision:
    """Outcome of evaluating + (optionally) executing a CVE auto-heal.

    ``executed`` is True only when the gate permitted the heal AND it ran.
    ``proposed`` is True when a fix is available but the gate withheld execution
    (SUPERVISED/ADVISORY, or auto-update pref off). ``confirmed`` reflects the
    re-scan: True when the post-heal scan shows no remaining HIGH/CRITICAL CVEs.
    """

    app_key: str
    executed: bool = False
    proposed: bool = False
    confirmed: bool = False
    reason: str = ""


def _auto_update_allowed(app_key: str) -> bool:
    """REUSE updates.py preferences: auto-update is allowed only when the app is
    not pinned and not notify-only.

    Defaults (notify_only=True, slop pinned) deliberately withhold auto-update —
    the operator must opt in per app.
    """
    try:
        from backend.api.updates import _default_pref, _load_prefs
        from backend.core.state import StateDB

        with StateDB() as db:
            prefs = _load_prefs(db)
        pref = prefs.get(app_key, _default_pref(app_key))
        return not pref.get("notify_only", True) and not pref.get("pinned", False)
    except Exception as exc:
        log.debug("auto-update pref lookup failed for %s: %s", app_key, exc)
        return False


def evaluate_cve_heal(
    app_key: str,
    image_ref: str,
    operational_level: OperationalLevel,
) -> CveHealDecision:
    """Decide and (when gated open) execute the CVE auto-heal for one app.

    Gate (BOTH required to execute):
      1. The per-app auto-update preference permits it (not pinned, not notify-only).
      2. ``operational_level is AUTONOMOUS``.

    When either condition fails but a drift exists, the fix is PROPOSED, not run.
    On execution, REUSES ``_heal_pull_image`` then re-scans to confirm the fix.
    """
    pref_ok = _auto_update_allowed(app_key)
    level_ok = operational_level is OperationalLevel.AUTONOMOUS

    if not (pref_ok and level_ok):
        reason_bits = []
        if not pref_ok:
            reason_bits.append("auto-update pref off/pinned")
        if not level_ok:
            reason_bits.append(f"operational_level={operational_level.value}")
        return CveHealDecision(
            app_key=app_key,
            proposed=True,
            reason="; ".join(reason_bits),
        )

    # Gate open — REUSE the existing pull/restart heal action. No new pull path.
    from backend.health.checker import _heal_pull_image

    pulled = _heal_pull_image(app_key)
    if not pulled:
        return CveHealDecision(
            app_key=app_key,
            executed=False,
            reason="pull_image heal returned False (no newer image / pull failed)",
        )

    # Re-scan to confirm the CVEs cleared.
    rescan = scan_image(image_ref)
    confirmed = rescan.available and rescan.total == 0
    return CveHealDecision(
        app_key=app_key,
        executed=True,
        confirmed=confirmed,
        reason=(
            "cve cleared after re-pull"
            if confirmed
            else "re-pull applied; cve not yet confirmed cleared"
        ),
    )


__all__ = [
    "SELF_APP_KEY",
    "CveHealDecision",
    "CveScanResult",
    "evaluate_cve_heal",
    "reconcile_cve",
    "scan_image",
]
