"""backend/health/scheduler_autoapply.py

Safe-tier auto-apply governance cluster for the health scheduler.

Extracted from ``scheduler.py`` (#1302 linecount drain) — the cohesive
auto-apply gate + row-filter pipeline that decides which pending fixes the
scheduler may autonomously apply. ``_maybe_auto_apply_safe_fixes`` is the
``auto_fixes`` post-cycle probe; ``scheduler.py`` re-imports it (and the gate
helpers) so existing callers and tests resolve unchanged.

Design: fail-closed at every gate (any read/import error ⇒ skip the cycle,
ask-never-act), and route every real mutation through the SHARED governance
gate so the scheduler is metered like chat / MCP.
"""

from __future__ import annotations

from typing import Any

from backend.agent.registry import register_probe  # #980: probe self-registration
from backend.core.logging import get_logger

log = get_logger(__name__)


def _read_autoapply_gate() -> tuple[float, str | None] | None:
    """Read the auto-apply gate config, fail-closed.

    Returns ``(min_confidence_threshold, operational_level_raw)`` when auto-apply
    should proceed, or ``None`` to skip this cycle because:
      * the scheduler is PAUSED (scheduler/pause sets the
        ``scheduler_paused`` StateDB flag — the global kill switch);
      * auto-apply is not enabled;
      * the config/kill-switch could not be read (ANY error ⇒ do NOT act).
    """
    try:
        from backend.core.state import StateDB as _SDB

        with _SDB() as _db:
            paused_raw = _db.get_setting("scheduler_paused")
            enabled_raw = _db.get_setting("agent_autofix_enabled")
            conf_raw = _db.get_setting("agent_autofix_min_confidence")
            level_raw = _db.get_setting("agent_operational_level")
    except Exception as cfg_err:
        log.debug("_read_autoapply_gate: config read failed (skipping): %s", cfg_err)
        return None

    if str(paused_raw).lower() in ("1", "true", "yes"):
        log.info("auto-apply gate: scheduler is PAUSED (kill switch) — skipping this cycle")
        return None
    if not (str(enabled_raw).lower() in ("1", "true", "yes") if enabled_raw else False):
        return None
    try:
        threshold = float(conf_raw) if conf_raw else 0.9
    except (TypeError, ValueError):
        threshold = 0.9
    return threshold, level_raw


def _filter_preapproved_rows(rows: list[Any]) -> list[Any] | None:
    """Keep only rows whose action-tier is pre-approved for their app (N5 policy).

    The scheduler may autonomously apply a fix ONLY if its action's tier is
    pre-approved for that app. A non-pre-approved row is dropped from the
    autonomous path (it still surfaces in chat, where the operator's typed
    "do it" is the per-action approval). Fail-closed:
      * a per-row resolution error drops that row;
      * an inability to load the policy at all returns ``None`` so the caller
        skips the cycle entirely (ask, never act).
    """
    try:
        from backend.agent.apply import get_fix_type
        from backend.agent.policy import load_policy
        from backend.agent.registry import tier_for
    except Exception as imp_err:  # fail-closed: no policy machinery ⇒ skip cycle
        log.warning(
            "auto-apply gate: pre-approval policy unavailable (skipping cycle): %s", imp_err
        )
        return None

    policy = load_policy()
    out: list[Any] = []
    for row in rows:
        try:
            dc = (
                row["diagnosis_class"]
                if hasattr(row, "__getitem__")
                else getattr(row, "diagnosis_class", "")
            )
            app_key = row["app_key"] if hasattr(row, "__getitem__") else getattr(row, "app_key", "")
            fix_type = get_fix_type(dc)
            tier = tier_for(fix_type)
            if policy.is_pre_approved(tier, app_key):
                out.append(row)
            else:
                log.info(
                    "auto-apply gate: tier %d for %s/%s not pre-approved — skipping (chat-only)",
                    int(tier),
                    fix_type,
                    app_key,
                )
        except Exception as row_err:  # fail-closed per row
            log.debug("auto-apply gate: policy check failed for a row (dropping): %s", row_err)
    return out


def _filter_app_circuit_rows(rows: list[Any]) -> list[Any]:
    """Drop rows for apps whose per-app circuit breaker is closed (5 fixes/app/hour).

    One flapping app cannot exhaust the global budget for the whole fleet. The
    global cap is enforced by the caller; this only applies the per-app cap.
    """
    from backend.agent.circuit_breaker import check_app_circuit as _check_app_circuit

    _app_cb_cache: dict[str, bool] = {}
    _filtered_rows = []
    for _row in rows:
        _ak = _row["app_key"] if hasattr(_row, "__getitem__") else getattr(_row, "app_key", "")
        if _ak not in _app_cb_cache:
            _acb = _check_app_circuit(_ak, cap=5)
            _app_cb_cache[_ak] = _acb.open
            if not _acb.open:
                log.warning(
                    "autofix per-app circuit open for %s: %d/%d fixes in last hour",
                    _ak,
                    _acb.fixes_last_hour,
                    _acb.cap,
                )
        if _app_cb_cache[_ak]:
            _filtered_rows.append(_row)
    return _filtered_rows


def _filter_governance_rows(rows: list[Any], op_level: Any) -> list[Any] | None:
    """Invariant 9: route every would-be mutation through the SHARED governance
    gate so the scheduler is metered through the SAME accounting as chat / MCP.

    A row the gate denies (tier/policy/budget) is dropped here BEFORE apply — the
    scheduler is never an unmetered bypass. Fail-closed: if the shared gate cannot
    be consulted at all, return ``None`` so the caller skips execution this cycle.
    """
    try:
        from backend.agent.apply import get_fix_type as _get_fix_type
        from backend.agent.governance import authorize as _authorize
        from backend.agent.registry import tier_for as _tier_for

        _gated_rows = []
        for _row in rows:
            _ak = _row["app_key"] if hasattr(_row, "__getitem__") else getattr(_row, "app_key", "")
            _dc = (
                _row["diagnosis_class"]
                if hasattr(_row, "__getitem__")
                else getattr(_row, "diagnosis_class", "")
            )
            _aid = _get_fix_type(_dc)
            _gov = _authorize(
                action_id=_aid,
                app_key=_ak,
                tier=_tier_for(_aid),
                operational_level=op_level,
                pre_approved=True,  # the scheduler's authority is the AUTONOMOUS policy
            )
            if _gov.allow:
                _gated_rows.append(_row)
            else:
                log.info(
                    "autofix governance gate: skip fix_id=%s app=%s action=%s — %s",
                    _row["id"] if hasattr(_row, "__getitem__") else getattr(_row, "id", "?"),
                    _ak,
                    _aid,
                    _gov.reason,
                )
        return _gated_rows
    except Exception as _gov_err:
        # Fail-closed: if the shared gate cannot be consulted, do not execute.
        log.warning("autofix governance gate unavailable — skipping execution: %s", _gov_err)
        return None


@register_probe("auto_fixes", description="Safe-tier auto-apply pass (long)")
def _maybe_auto_apply_safe_fixes() -> None:
    """If agent_autofix_enabled, auto-apply eligible safe-tier pending fixes via
    apply_eligible_fixes (which enforces the confirmation gate + backoff + verify).
    Off by default. Never raises.

    Operational level is read from ``agent_operational_level`` setting:
      supervised (default) — dry_run=True gate; caller must opt in per invocation.
      autonomous           — gate bypassed; fixes execute when enabled=true.
    """
    # Kill switch + enabled + threshold (fail-closed; None ⇒ skip this cycle).
    _gate = _read_autoapply_gate()
    if _gate is None:
        return
    _threshold, _level_raw = _gate

    from backend.agent.circuit_breaker import check_circuit as _check_circuit

    _cb = _check_circuit(cap=10)
    if not _cb.open:
        log.warning(
            "autofix circuit open: %d/%d fixes in last hour",
            _cb.fixes_last_hour,
            _cb.cap,
        )
        return

    # #867: gated CVE auto-heal rides the SAME kill-switch + circuit breaker as
    # every other auto-fix. Self-isolated + never-raises; each app is PROPOSED
    # (not executed) unless its auto-update pref is on AND the level is AUTONOMOUS.
    # Drives off the persisted health.cve.* DRIFT findings (no re-scan).
    from backend.agent.cve_audit import run_cve_auto_heal
    from backend.agent.types import OperationalLevel

    run_cve_auto_heal(OperationalLevel.from_setting(_level_raw))

    try:
        from backend.agent.autofix import select_auto_applicable, apply_eligible_fixes

        _rows = select_auto_applicable(min_confidence=_threshold)
    except Exception as _sel_err:
        log.debug("_maybe_auto_apply_safe_fixes: select failed: %s", _sel_err)
        return

    # Per-app circuit breaker: filter out fixes for apps that have hit their
    # per-app cap (5 fixes/app/hour). The global cap above remains intact.
    _rows = _filter_app_circuit_rows(_rows)

    # N5 pre-approval policy (tier x scope) — fail-closed; None signals "skip cycle".
    _preapproved = _filter_preapproved_rows(_rows)
    if _preapproved is None:
        return
    _rows = _preapproved

    _op_level = OperationalLevel.from_setting(_level_raw)
    # Gate: only AUTONOMOUS bypasses dry_run; SUPERVISED (default) keeps gate ON.
    _dry_run = _op_level is not OperationalLevel.AUTONOMOUS

    # Invariant 9: route every would-be mutation through the SHARED governance gate
    # so the scheduler is metered through the SAME accounting as chat / MCP. Only
    # filter for real (non-dry-run) execution; dry-run rows are advisory and left
    # intact for the report. Fail-closed: None ⇒ skip execution this cycle.
    if not _dry_run:
        _gated = _filter_governance_rows(_rows, _op_level)
        if _gated is None:
            return
        _rows = _gated

    _results = apply_eligible_fixes(_rows, dry_run=_dry_run, operational_level=_op_level)
    for _res in _results:
        if _res.get("dry_run"):
            log.info(
                "autofix gate: DRY-RUN fix_id=%s app=%s — %s",
                _res["fix_id"],
                _res["app_key"],
                _res["message"],
            )
        else:
            log.info(
                "AUTO-applied fix_id=%s app=%s ok=%s: %s",
                _res["fix_id"],
                _res["app_key"],
                _res.get("ok"),
                _res.get("message", ""),
            )
