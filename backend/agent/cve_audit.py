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


def image_ref_for_key(app_key: str) -> str:
    """Resolve the scanned image ref for an ``app_key`` (or ``SELF_APP_KEY``).

    Public seam for the scheduler auto-heal wiring (#867): it lets the apply
    pipeline feed ``evaluate_cve_heal`` from a persisted ``health.cve.<key>``
    finding without importing this module's private image-ref helpers. Returns
    ``''`` when the key is unknown or declares no image (the caller then skips it).
    """
    if app_key == SELF_APP_KEY:
        return _self_image_ref()
    try:
        from backend.manifests.loader import load_manifest

        app = load_manifest(app_key)
    except Exception as exc:
        log.debug("image_ref_for_key: manifest load failed for %s: %s", app_key, exc)
        return ""
    return _image_ref_for(app) if app is not None else ""


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

    # Domain gate open (pref + AUTONOMOUS). Route through the shared authorize() chokepoint
    # (#1236 / #977 / DToC consensus Q3 Option A) so CVE-heal — the last mutating path that
    # bypassed authorize() — now also counts against the shared per-app/global action budget
    # (the leg 2-1b's bypass-audit flagged missing). The per-app auto-update pref is the domain
    # pre-approval; the level was already checked AUTONOMOUS above. Reuses the registry
    # repull_restart action (T2 RECOVERABLE) — _heal_pull_image IS a re-pull+restart. Fail-closed:
    # any gate error withholds the heal (executed=False), never an unmetered pull.
    try:
        from backend.agent.governance import authorize
        from backend.agent.registry import tier_for

        gov = authorize(
            action_id="repull_restart",
            app_key=app_key,
            tier=tier_for("repull_restart"),
            operational_level=operational_level,
            pre_approved=True,
        )
    except Exception as gov_err:
        return CveHealDecision(
            app_key=app_key,
            executed=False,
            reason=f"governance gate unavailable (fail-closed): {gov_err}",
        )
    if not gov.allow:
        return CveHealDecision(
            app_key=app_key,
            executed=False,
            reason=f"governance gate withheld CVE-heal: {gov.reason}",
        )

    # REUSE the existing pull/restart heal action. No new pull path. Wrapped in the
    # agent-action audit trail (#1072): QUEUED before the pull, OUTCOME + notify after
    # (both fail-open, never raise into the heal path).
    from backend.agent.audit import notify_action, record_action_outcome, record_action_queued
    from backend.health.checker import _heal_pull_image

    _heal_tier = int(tier_for("repull_restart"))
    run_id = record_action_queued(
        trigger="scheduler", action_id="repull_restart", app_key=app_key, tier=_heal_tier
    )
    audit_status, audit_msg = "failed", ""
    try:
        pulled = _heal_pull_image(app_key)
        if not pulled:
            audit_msg = "no newer image / pull failed"
            return CveHealDecision(
                app_key=app_key,
                executed=False,
                reason="pull_image heal returned False (no newer image / pull failed)",
            )
        # The repull_restart action executed (success is action-level, independent of
        # whether the re-scan confirms the CVEs cleared).
        audit_status = "ok"

        # Re-scan to confirm the CVEs cleared.
        rescan = scan_image(image_ref)
        confirmed = rescan.available and rescan.total == 0
        audit_msg = f"re-pull applied; cve_confirmed={confirmed}"
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
    finally:
        try:
            record_action_outcome(run_id, status=audit_status, outcome_msg=audit_msg)
            notify_action(
                action_id="repull_restart",
                app_key=app_key,
                tier=_heal_tier,
                status=audit_status,
                outcome_msg=audit_msg,
            )
        except Exception as _ae:  # pragma: no cover - defensive; both calls fail-open
            log.warning("evaluate_cve_heal: audit/notify failed for run_id=%s: %s", run_id, _ae)


def run_cve_auto_heal(
    operational_level: OperationalLevel | None = None,
) -> list[CveHealDecision]:
    """Gated CVE auto-heal cycle (#867) — the wiring that makes ``evaluate_cve_heal``
    actually run from the scheduler's auto-apply pipeline.

    For each app carrying a persisted ``health.cve.<key>`` DRIFT finding (written
    by :func:`reconcile_cve` in the ``_run_cve_probes`` post-cycle pass — NO re-scan
    here; since that pass and this one run concurrently under the cycle's
    ``asyncio.gather``, the findings read here are the PREVIOUS pass's, a safe
    one-cycle lag), evaluate the gated heal. ``evaluate_cve_heal`` is the per-app
    gate (executes only when the
    app's auto-update pref is on AND ``operational_level is AUTONOMOUS``; otherwise
    PROPOSED). ``operational_level`` defaults to the live ``agent_operational_level``
    setting. NEVER raises; returns the per-app decisions (empty when nothing is
    drifted or on any setup failure — the scheduler must not break on a heal error).
    """
    decisions: list[CveHealDecision] = []
    try:
        from backend.agent.spine import HEALTH_SUBJECT_TYPE, VERDICT_TO_HEALTH_STATUS
        from backend.core.state import StateDB

        if operational_level is None:
            with StateDB() as db:
                operational_level = OperationalLevel.from_setting(
                    db.get_setting("agent_operational_level")
                )
        drift_status = VERDICT_TO_HEALTH_STATUS[Verdict.DRIFT]
        with StateDB() as db:
            rows = db.get_health_checks(subject_type=HEALTH_SUBJECT_TYPE)
    except Exception as exc:  # the cycle must never break the scheduler
        log.debug("cve auto-heal: setup/query failed: %s", exc)
        return decisions

    prefix = "health.cve."
    for hc in rows:
        key = getattr(hc, "subject_key", "") or ""
        if not key.startswith(prefix) or getattr(hc, "status", "") != drift_status:
            continue
        app_key = key[len(prefix) :]
        ref = image_ref_for_key(app_key)
        if not ref:
            log.debug("cve auto-heal: no image ref for drifted app %s — skipping", app_key)
            continue
        try:
            decision = evaluate_cve_heal(app_key, ref, operational_level)
        except Exception as exc:
            log.debug("cve auto-heal raised for %s: %s", app_key, exc)
            continue
        decisions.append(decision)
        if decision.executed:
            log.info(
                "CVE auto-heal app=%s EXECUTED confirmed=%s: %s",
                app_key,
                decision.confirmed,
                decision.reason,
            )
        elif decision.proposed:
            log.info("CVE auto-heal app=%s PROPOSED (gate withheld): %s", app_key, decision.reason)
        else:
            log.info("CVE auto-heal app=%s no-op: %s", app_key, decision.reason)
    return decisions


__all__ = [
    "SELF_APP_KEY",
    "CveHealDecision",
    "CveScanResult",
    "evaluate_cve_heal",
    "image_ref_for_key",
    "reconcile_cve",
    "run_cve_auto_heal",
    "scan_image",
]
