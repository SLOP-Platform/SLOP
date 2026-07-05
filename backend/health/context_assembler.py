"""backend/health/context_assembler.py

Assembles rich diagnostic context for LLM prompts.

Queries the DB for everything relevant to the failing app and check,
then formats it as a structured context block injected into the prompt.

The orchestrator `_build` calls a sequence of `_section_*` helpers,
each responsible for one numbered section of the context block.
Helpers append to the shared `lines` list and handle their own
exceptions — a failure in one section never aborts the whole build.
"""

from __future__ import annotations

from typing import Any

import json
import time
from datetime import datetime, UTC

from backend.core.logging import get_logger

log = get_logger(__name__)
_DAYS = 7


def assemble_context(app_key: str, check_name: str, runtime: dict[str, Any] | None = None) -> str:
    """Return a formatted context block for the LLM prompt.
    runtime: optional dict with keys restart_count, exit_code, oom_killed,
             config_disk_pct, network_checks
    Never raises — returns empty string on any failure.
    """
    try:
        return _build(app_key, check_name, runtime or {})
    except Exception as e:
        log.debug("context_assembler failed: %s", e)
        return ""


def _build(app_key: str, check_name: str, runtime: dict[str, Any]) -> str:
    from backend.core.state import StateDB

    cutoff = int(time.time()) - _DAYS * 86400
    lines: list[str] = ["=== DIAGNOSTIC CONTEXT ==="]

    with StateDB() as db:
        _section_health_interval(db, lines)
        _section_app_record(db, app_key, lines)
        _section_runtime_state(runtime, lines)
        _section_operation_history(db, app_key, cutoff, lines)
        _section_install_steps(db, app_key, lines)
        _section_check_path(app_key, lines)
        _section_check_type(db, app_key, check_name, lines)
        _section_health_history(db, app_key, check_name, cutoff, lines)
        _section_failure_duration(db, app_key, check_name, lines)
        _section_fix_history(db, app_key, cutoff, lines)
        _section_pending_fixes(db, app_key, check_name, lines)
        _section_notifications(db, lines)
        _section_prev_llm_diagnoses(db, app_key, cutoff, lines)
        _section_app_wiring(db, app_key, lines)
        _section_service_deps(db, app_key, lines)
        _section_storage(db, lines)
        _section_external_resources(db, app_key, lines)
        _section_traefik(lines)
        _section_infra_overview(db, lines)
        _section_maintenance(db, app_key, check_name, lines)
        _section_rolling_failure(db, lines)
        _section_mass_failure(db, lines)
        _section_correlated(db, app_key, lines)
        _section_network_checks(runtime, lines)
        _section_system_profile(db, lines)
        _section_data_disk(lines)
        _section_source_availability(db, app_key, lines)
        _section_app_category(app_key, lines)
        _section_compose_fragment(app_key, lines)
        _section_ms_test(app_key, lines)
        _section_infra_app_health(db, lines)
        _section_ms_update_context(lines)
        _section_ms_update_overdue(lines)

    lines.append("=== END CONTEXT ===")
    return "\n".join(lines)


# ── 0. Health check interval + Docker daemon health ─────────────────
def _section_health_interval(db: Any, lines: list[str]) -> None:
    try:
        interval_s = int(db.get_setting("health_check_interval_secs") or 30)
        lines.append(f"Health check interval: {interval_s}s")
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)
    try:
        slow_ms = int(db.get_setting("docker_daemon_slow_ms") or 0)
        if slow_ms > 3000:
            lines.append(
                f"⚠ DOCKER DAEMON SLOW: last response {slow_ms}ms. "
                f"Health check results may be false positives — Docker is under load."
            )
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 1. App record ───────────────────────────────────────────────────
def _section_app_record(db: Any, app_key: str, lines: list[str]) -> None:
    app = db.get_app(app_key)
    if app and getattr(app, "manifest_source", None) == "custom":
        lines.append(
            "CUSTOM APP: Installed outside catalog. Health check parameters "
            "(port, HTTP path, startup time) may be misconfigured. "
            "Standard health check failure diagnosis may not apply."
        )
    elif app and getattr(app, "manifest_source", None) == "custom_enhanced":
        lines.append(
            "ENHANCED CUSTOM APP: Installed outside catalog but health check "
            "parameters have been manually configured — treat as a managed app."
        )
    if app:
        age_days = (time.time() - (app.installed_at or time.time())) / 86400
        status_note = ""
        if app.status == "failed":
            status_note = " ⚠ INSTALL FAILED — app never ran successfully"
        elif app.status == "error":
            status_note = " ⚠ RUNTIME ERROR"
        lines.append(
            f"App: {app.display_name} ({app_key}) | "
            f"Status: {app.status}{status_note} | Port: {app.host_port or 'none'} | "
            f"Installed: {int(age_days)}d ago"
        )


# ── 2. Runtime container state (Pass 2 data) ────────────────────────
def _section_runtime_state(runtime: dict[str, Any], lines: list[str]) -> None:
    if not runtime:
        return
    parts = []
    rc = runtime.get("restart_count")
    if rc is not None:
        parts.append(f"restarts: {rc}" + (" ⚠ crash-looping" if rc > 10 else ""))
    ec = runtime.get("exit_code")
    if ec is not None and ec != 0:
        parts.append(f"last exit code: {ec}")
    if runtime.get("oom_killed"):
        parts.append("OOM killed: YES ⚠ out of memory — increase RAM or reduce container limit")
    if parts:
        lines.append("Container state: " + " | ".join(parts))
    disk_pct = runtime.get("config_disk_pct")
    if disk_pct and disk_pct > 80:
        lines.append(f"Config disk: {disk_pct}% full ⚠ disk pressure may cause failures")


# ── 3. Operation history ────────────────────────────────────────────
def _section_operation_history(db: Any, app_key: str, cutoff: int, lines: list[str]) -> None:
    try:
        ops = db.execute(
            """SELECT operation, status, error, started_at
               FROM operations
               WHERE subject_key = ? AND started_at >= ?
               ORDER BY started_at DESC LIMIT 10""",
            (app_key, cutoff),
        ).fetchall()
        if ops:
            lines.append("Recent operations:")
            for op in ops:
                ts = datetime.fromtimestamp(op["started_at"], tz=UTC).strftime("%m-%d %H:%M")
                err = f" — {op['error'][:70]}" if op["error"] else ""
                lines.append(f"  {ts} {op['operation']}: {op['status']}{err}")
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 4. Last install/update steps (operation_steps) ──────────────────
def _section_install_steps(db: Any, app_key: str, lines: list[str]) -> None:
    try:
        last_op = db.execute(
            """SELECT id FROM operations
               WHERE subject_key = ? AND operation IN ('install','update')
               ORDER BY started_at DESC LIMIT 1""",
            (app_key,),
        ).fetchone()
        if last_op:
            steps = db.execute(
                """SELECT step_name, status, message
                   FROM operation_steps WHERE op_key = ?
                   ORDER BY created_at""",
                (app_key,),
            ).fetchall()
            failed_steps = [s for s in steps if s["status"] in ("error", "warning")]
            if failed_steps:
                lines.append("Last install/update had failed steps:")
                for s in failed_steps[:3]:
                    lines.append(f"  {s['step_name']} [{s['status']}]: {s['message'][:80]}")
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 4b-i. Health check path (when non-default) ──────────────────────
def _section_check_path(app_key: str, lines: list[str]) -> None:
    try:
        from backend.manifests.loader import load_manifest as _lm

        _m = _lm(app_key)
        _http_checks = [h for h in _m.health_checks if h.check_type == "http"]
        if _http_checks and _http_checks[0].path and _http_checks[0].path != "/":
            lines.append(f"Health check path: {_http_checks[0].path}")
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 4b-ii. Check type — flag non-HTTP checks (TCP/process) ──────────
def _section_check_type(db: Any, app_key: str, check_name: str, lines: list[str]) -> None:
    try:
        check_meta = db.execute(
            """SELECT check_type FROM health_checks
               WHERE subject_key = ? AND check_name = ?
               LIMIT 1""",
            (app_key, check_name),
        ).fetchone()
        if check_name in ("tcp_reachable", "tcp_check") or (
            check_meta and "tcp" in str(check_meta)
        ):
            lines.append(
                "Check type: TCP port reachability — failure means port not open, "
                "not an application logic error. Check docker logs for startup failure."
            )
        elif check_name in ("process_running", "process_check"):
            lines.append(
                "Check type: process/container running state — failure means container "
                "exited or crash-looped. Exit code and OOM data above are primary signals."
            )
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 5a. Health check history — failure pattern ──────────────────────
def _section_health_history(
    db: Any, app_key: str, check_name: str, cutoff: int, lines: list[str]
) -> None:
    try:
        hist = db.execute(
            """SELECT status, summary, checked_at
               FROM health_check_history
               WHERE subject_key = ? AND check_name = ? AND checked_at >= ?
               ORDER BY checked_at DESC LIMIT 20""",
            (app_key, check_name, cutoff),
        ).fetchall()
        if hist:
            failures = [h for h in hist if h["status"] in ("error", "warning")]
            hours = [datetime.fromtimestamp(h["checked_at"], tz=UTC).hour for h in failures]
            typical_hour = max(set(hours), key=hours.count) if hours else None
            lines.append(
                f"Check '{check_name}': {len(failures)}/{len(hist)} failures in last {_DAYS}d"
                + (f", often ~{typical_hour:02d}:00 UTC" if typical_hour is not None else "")
            )
            last_err = next((h["summary"] for h in hist if h["status"] == "error"), None)
            if last_err:
                lines.append(f"  Last error: {last_err[:100]}")
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 5b. Duration of current failure streak ──────────────────────────
def _section_failure_duration(db: Any, app_key: str, check_name: str, lines: list[str]) -> None:
    try:
        _last_ok = db.execute(
            """SELECT MAX(checked_at) FROM health_check_history
               WHERE subject_key=? AND check_name=? AND status='ok'""",
            (app_key, check_name),
        ).fetchone()
        if _last_ok and _last_ok[0]:
            _fail_mins = int((time.time() - _last_ok[0]) / 60)
            if _fail_mins < 60:
                lines.append(f"Failing for: {_fail_mins} minutes (last ok {_fail_mins}m ago)")
            elif _fail_mins < 1440:
                lines.append(
                    f"Failing for: {_fail_mins // 60}h {_fail_mins % 60}m — sustained failure"
                )
            else:
                lines.append(
                    f"Failing for: {_fail_mins // 1440}d — CHRONIC failure, likely config issue not runtime"
                )
        else:
            lines.append("Failing for: unknown duration (no successful check on record)")
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 6. Fix history ──────────────────────────────────────────────────
def _section_fix_history(db: Any, app_key: str, cutoff: int, lines: list[str]) -> None:
    try:
        fixes = db.execute(
            """SELECT error_type, suggested_fix, outcome, created_at, rejection_reason
               FROM fix_history
               WHERE app_key = ? AND created_at >= ?
               ORDER BY created_at DESC LIMIT 10""",
            (app_key, cutoff),
        ).fetchall()
        if fixes:
            lines.append("Previous fix attempts:")
            for f in fixes:
                ts = datetime.fromtimestamp(f["created_at"], tz=UTC).strftime("%m-%d")
                icon = (
                    "✓"
                    if f["outcome"] == "success"
                    else "✗"
                    if f["outcome"] == "failure"
                    else "👤"
                    if f["outcome"] == "user_approved_manual"
                    else "⏳"
                    if f["outcome"] == "pending"
                    else "?"
                )
                suffix = ""
                if f["outcome"] == "user_approved_manual":
                    suffix = " — user ran manually, outcome unknown"
                elif f["outcome"] == "pending":
                    suffix = " — fix applied but not yet verified"
                if f["outcome"] == "failure" and f["rejection_reason"]:
                    suffix = f" — rejected: {f['rejection_reason'][:60]}"
                lines.append(f"  {ts} [{icon}] {f['suggested_fix'][:80]}{suffix}")
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 6b-i. Pending fixes — avoid duplicate suggestions ───────────────
def _section_pending_fixes(db: Any, app_key: str, check_name: str, lines: list[str]) -> None:
    try:
        pending = db.execute(
            """SELECT action_type, problem, suggested_fix, confidence, model, created_at
               FROM pending_fixes
               WHERE app_key = ? AND check_name = ? AND status = 'pending'
               ORDER BY created_at DESC LIMIT 2""",
            (app_key, check_name),
        ).fetchall()
        if pending:
            lines.append("Already pending user approval:")
            for p in pending:
                ts = datetime.fromtimestamp(p["created_at"], tz=UTC).strftime("%m-%d")
                lines.append(
                    f"  {ts} [{p['action_type']}] {p['suggested_fix'][:80]} "
                    f"(confidence {p['confidence']:.0%}) — DO NOT suggest again, "
                    f"it is awaiting user approval."
                )
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 6b-ii. Notification delivery status ─────────────────────────────
def _section_notifications(db: Any, lines: list[str]) -> None:
    try:
        _ntfy_url = db.get_setting("ntfy_url") or "http://ntfy:80"
        _ntfy_enabled = (db.get_setting("ntfy_enabled") or "false") == "true"
        if _ntfy_enabled:
            _ntfy_app = db.execute("SELECT status FROM apps WHERE key='ntfy'").fetchone()
            if _ntfy_app and _ntfy_app["status"] == "running":
                lines.append(
                    f"Notifications: ntfy running at {_ntfy_url} — users are being notified"
                )
            elif _ntfy_app:
                lines.append(
                    f"⚠ Notifications: ntfy is {_ntfy_app['status']} — users may NOT be receiving alerts"
                )
            else:
                lines.append(
                    f"Notifications: enabled (url={_ntfy_url}) but ntfy not in app registry — delivery unknown"
                )
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# NOTE (#1164): rejection-learning is now wired — rejection_reason is captured
# on fix_history rows (migration 024) and surfaced in _section_fix_history above.
# Suppress-after-3 remains unbuilt: it needs a SEPARATE rejection signal that
# preserves the failure-tally contract.


# ── 7. Previous LLM diagnoses (routing log) ─────────────────────────
def _section_prev_llm_diagnoses(db: Any, app_key: str, cutoff: int, lines: list[str]) -> None:
    try:
        prev_diags = db.execute(
            """SELECT model, summary, success, ts
               FROM llm_routing_log
               WHERE app_key = ? AND ts >= ?
               ORDER BY ts DESC LIMIT 10""",
            (app_key, cutoff),
        ).fetchall()
        if prev_diags:
            lines.append("Previous LLM diagnoses:")
            for d in prev_diags:
                ts = datetime.fromtimestamp(d["ts"], tz=UTC).strftime("%m-%d")
                icon = "✓" if d["success"] else "✗"
                summary = (d["summary"] or "")[:80]
                lines.append(f"  {ts} [{icon}] {summary}")
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 8. App wiring — dependencies between apps ───────────────────────
def _section_app_wiring(db: Any, app_key: str, lines: list[str]) -> None:
    try:
        wirings = db.execute(
            """SELECT a2.key as target_key, a2.display_name as target_name,
                      w.wire_type, w.status
               FROM wiring w
               JOIN apps a1 ON a1.id = w.source_app_id
               JOIN apps a2 ON a2.id = w.target_app_id
               WHERE a1.key = ?""",
            (app_key,),
        ).fetchall()
        if wirings:
            lines.append("App wiring:")
            for w in wirings:
                flag = " ⚠ STALE/FAILED" if w["status"] in ("failed", "stale") else ""
                lines.append(f"  → {w['target_name']} ({w['wire_type']}, {w['status']}){flag}")
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 9. Database/service dependencies ────────────────────────────────
def _section_service_deps(db: Any, app_key: str, lines: list[str]) -> None:
    try:
        deps = db.execute(
            """SELECT d.dependency_type, d.db_name, ms.status as svc_status
               FROM app_dependencies d
               JOIN apps a ON a.id = d.app_id
               LEFT JOIN managed_services ms ON ms.service_type = d.dependency_type
               WHERE a.key = ?""",
            (app_key,),
        ).fetchall()
        if deps:
            lines.append("Service dependencies:")
            for d in deps:
                flag = " ⚠ SERVICE DOWN" if d["svc_status"] == "error" else ""
                db_info = f" (db: {d['db_name']})" if d["db_name"] else ""
                lines.append(
                    f"  {d['dependency_type']}: {d['svc_status'] or 'unknown'}{db_info}{flag}"
                )
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 10. Storage sources status ──────────────────────────────────────
def _section_storage(db: Any, lines: list[str]) -> None:
    try:
        storages = db.execute(
            "SELECT name, source_type, mount_point, status, error_message FROM storage_sources"
        ).fetchall()
        bad_storage = [s for s in storages if s["status"] in ("error", "inactive")]
        if bad_storage:
            lines.append("Storage issues:")
            for s in bad_storage:
                err = f": {s['error_message'][:60]}" if s["error_message"] else ""
                lines.append(
                    f"  {s['name']} ({s['source_type']}) at {s['mount_point']} — {s['status']}{err}"
                )
            lines.append("  → Storage failure often causes simultaneous failures across arr apps")
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 11. External resources (CF hostnames) ───────────────────────────
def _section_external_resources(db: Any, app_key: str, lines: list[str]) -> None:
    try:
        ext = db.execute(
            """SELECT resource_type, hostname, removed_at
               FROM external_resources er
               JOIN apps a ON a.id = er.app_id
               WHERE a.key = ? AND er.removed_at IS NULL""",
            (app_key,),
        ).fetchall()
        if ext:
            lines.append("External resources:")
            for r in ext:
                lines.append(f"  {r['resource_type']}: {r['hostname']}")
        else:
            lines.append("External resources: none provisioned (app may not be exposed externally)")
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 11b. Traefik status ─────────────────────────────────────────────
def _section_traefik(lines: list[str]) -> None:
    try:
        from backend.core import docker_client as _dc2

        _t = _dc2.get_container("traefik")
        if _t:
            if _t.status != "running":
                lines.append(
                    f"⚠ TRAEFIK IS {_t.status.upper()} — reverse proxy is down. "
                    f"All external app access broken. Apps may appear unreachable "
                    f"even if containers are healthy."
                )
        else:
            from backend.core.config import config as _cfg3

            if (_cfg3.compose_dir / "traefik.yaml").exists():
                lines.append(
                    "⚠ TRAEFIK CONTAINER MISSING — compose fragment exists but "
                    "container not found. Run: docker compose -f data/compose/traefik.yaml up -d"
                )
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 12. Infrastructure overview (handles multi-tunnel) ──────────────
def _section_infra_overview(db: Any, lines: list[str]) -> None:
    try:
        slots = db.execute("SELECT slot, provider, status FROM infra_slots").fetchall()
        active = [
            f"{s['slot']}={s['provider']}"
            for s in slots
            if s["status"] == "active" and s["provider"]
        ]
        try:
            tunnels = db.execute(
                "SELECT provider, status FROM infra_tunnel_providers WHERE status='active'"
            ).fetchall()
            if tunnels:
                tunnel_names = [t["provider"] for t in tunnels]
                active = [a for a in active if not a.startswith("tunnel=")]
                active.append("tunnels=" + "+".join(tunnel_names))
        except Exception as e:
            log.debug("diagnostic context section skipped: %s", e)
        if active:
            lines.append("Active infra: " + ", ".join(active))
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 13. Maintenance windows ─────────────────────────────────────────
def _section_maintenance(db: Any, app_key: str, check_name: str, lines: list[str]) -> None:
    try:
        mw = db.execute(
            """SELECT label, day_of_week, hour_start, hour_end
               FROM maintenance_windows
               WHERE app_key = ? AND check_name = ? AND enabled = 1""",
            (app_key, check_name),
        ).fetchall()
        if mw:
            day_names = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
            for w in mw:
                day_str = day_names[w["day_of_week"]] if w["day_of_week"] is not None else "daily"
                end_h = w["hour_end"] if w["hour_end"] != -1 else w["hour_start"] + 2
                lines.append(
                    f"Maintenance window: {w['label']} — {day_str} "
                    f"{w['hour_start']:02d}:00-{end_h:02d}:00 UTC (failure likely expected)"
                )
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 13b-i. Rolling failure detection (gradual cascade) ──────────────
def _section_rolling_failure(db: Any, lines: list[str]) -> None:
    try:
        _rolling_window = int(time.time()) - 1800  # 30 minutes
        _new_failures = db.execute(
            """SELECT COUNT(DISTINCT subject_key) as n FROM health_check_history
               WHERE status='error' AND checked_at >= ?
               AND subject_key NOT IN (
                   SELECT subject_key FROM health_check_history
                   WHERE status='error' AND checked_at < ?
                   AND checked_at >= ?
               )""",
            (_rolling_window, _rolling_window, int(time.time()) - 7200),
        ).fetchone()
        if _new_failures and _new_failures["n"] >= 3:
            lines.append(
                f"⚠ ROLLING FAILURE: {_new_failures['n']} apps newly failing in last 30min. "
                f"This pattern suggests gradual infrastructure degradation — "
                f"check Traefik, Docker daemon, managed services, and storage."
            )
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 13b-ii. Mass failure detection ──────────────────────────────────
def _section_mass_failure(db: Any, lines: list[str]) -> None:
    try:
        now_ts = int(time.time())
        five_min = now_ts - 300
        mass = db.execute(
            """SELECT COUNT(DISTINCT subject_key) as n FROM health_checks
               WHERE status IN ('error','warning') AND checked_at >= ?""",
            (five_min,),
        ).fetchone()
        if mass and mass["n"] >= 4:
            lines.append(
                f"⚠ MASS FAILURE EVENT: {mass['n']} apps failing simultaneously. "
                f"This is almost certainly an infrastructure-level event "
                f"(Docker daemon, managed service, storage, or network). "
                f"Do NOT diagnose individual apps — find and fix the root cause first."
            )
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 14. Correlated failures ─────────────────────────────────────────
def _section_correlated(db: Any, app_key: str, lines: list[str]) -> None:
    try:
        recent_window = int(time.time()) - 300
        others = db.execute(
            """SELECT subject_key, check_name
               FROM health_checks
               WHERE subject_key != ? AND status IN ('error','warning')
                 AND checked_at >= ?
               ORDER BY checked_at DESC LIMIT 8""",
            (app_key, recent_window),
        ).fetchall()
        if others:
            other_keys = list(dict.fromkeys(o["subject_key"] for o in others))
            lines.append(
                f"Also failing right now: {', '.join(other_keys[:5])}"
                + (" — possible shared root cause" if len(other_keys) > 2 else "")
            )
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 15. Network reachability checks (Pass 2 data) ───────────────────
def _section_network_checks(runtime: dict[str, Any], lines: list[str]) -> None:
    net_checks = runtime.get("network_checks", {})
    if not net_checks:
        return
    lines.append("Network reachability from container:")
    for target, ok in net_checks.items():
        lines.append(f"  {'✓' if ok else '✗'} {target}")


# ── 16. System profile — full fingerprint ───────────────────────────
def _section_system_profile(db: Any, lines: list[str]) -> None:
    try:
        profile_raw = db.get_setting("system_profile")
        if not profile_raw:
            return
        p = json.loads(profile_raw)

        ram = p.get("ram", {})
        cpu = p.get("cpu", {})
        gpu_list = p.get("gpu", [])
        gpu = gpu_list[0] if gpu_list else {}
        docker_info = p.get("docker", {})
        os_info = p.get("os", {})
        user_info = p.get("user", {})

        total_gb = ram.get("total_gb") or p.get("total_ram_gb", 0)
        avail_gb = ram.get("available_gb") or p.get("available_ram_gb", 0)
        cpu_model = cpu.get("model") or p.get("cpu_model", "")
        cpu_cores = cpu.get("cores") or p.get("cpu_cores", 0)

        _profile_os(os_info, lines)
        _profile_cpu(cpu, cpu_model, cpu_cores, lines)
        _profile_ram(total_gb, avail_gb, lines)
        _profile_gpu(gpu, lines)
        _profile_docker(docker_info, total_gb, lines)
        _profile_user(user_info, lines)

        tz = p.get("timezone") or user_info.get("timezone", "")
        if tz:
            lines.append(f"Host timezone: {tz}")
        if p.get("server_ip"):
            lines.append(f"Server LAN IP: {p['server_ip']}")
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


def _profile_os(os_info: dict[str, Any], lines: list[str]) -> None:
    if not os_info.get("distro"):
        return
    arch = os_info.get("arch", "")
    lines.append(
        f"Host OS: {os_info['distro']} {os_info.get('version', '')} "
        f"({arch}) · kernel {os_info.get('kernel', '')}"
    )
    if arch in ("arm64", "armv7", "aarch64"):
        lines.append(
            "  ⚠ ARM architecture — Docker images without ARM builds "
            "will fail to pull with 'no matching manifest' error."
        )


def _profile_cpu(cpu: dict[str, Any], cpu_model: str, cpu_cores: int, lines: list[str]) -> None:
    avx2 = cpu.get("avx2", True)
    cpu_str = f"{cpu_model[:40]} · {cpu_cores} cores" if cpu_cores else cpu_model[:40]
    if cpu_str:
        lines.append(f"CPU: {cpu_str}")
    if not avx2:
        lines.append(
            "  ⚠ CPU lacks AVX2 — llama.cpp and Ollama WILL FAIL TO START "
            "on this CPU. Use a cloud LLM provider instead."
        )


def _profile_ram(total_gb: float, avail_gb: float, lines: list[str]) -> None:
    lines.append(f"RAM: {total_gb:.0f}GB total, {avail_gb:.1f}GB available")
    if avail_gb < 1.0:
        lines.append("  ⚠ RAM CRITICALLY LOW — OOM likely root cause")
    elif avail_gb < 2.0:
        lines.append("  ⚠ RAM low — container restarts may be memory pressure")


def _profile_gpu(gpu: dict[str, Any], lines: list[str]) -> None:
    if not gpu.get("model"):
        return
    vram_gb = gpu.get("vram_gb", 0)
    vendor = (gpu.get("vendor") or "").lower()
    cuda = gpu.get("cuda", "")
    backend = gpu.get("backend", "")

    if vendor == "nvidia":
        api_str = f" · CUDA {cuda}" if cuda else " · no CUDA detected (check nvidia-smi)"
    elif vendor in ("amd", "ati"):
        api_str = f" · ROCm {backend}" if backend else " · ROCm (check rocm-smi)"
    elif vendor == "apple":
        api_str = " · Metal (Apple Silicon)"
    elif vendor == "intel":
        api_str = " · Intel GPU (limited inference support)"
    else:
        api_str = ""

    lines.append(f"GPU: {gpu['model']} · {vram_gb}GB VRAM{api_str}")
    if gpu.get("inference_capable") and vram_gb > 0:
        lines.append(
            f"  GPU inference capable — if Ollama/llama.cpp is OOM-killing, "
            f"the model may exceed {vram_gb}GB VRAM. Check: ollama ps"
        )


def _profile_docker(docker_info: dict[str, Any], total_gb: float, lines: list[str]) -> None:
    if not docker_info.get("engine"):
        return
    n_containers = docker_info.get("containers_running", 0)
    lines.append(
        f"Docker: v{docker_info['engine']} "
        f"(Compose {docker_info.get('compose', '?')}) · "
        f"{n_containers} containers running"
    )
    if total_gb > 0 and n_containers / total_gb > 2.5:
        lines.append(
            f"  ⚠ CONTAINER SATURATION: {n_containers} containers on "
            f"{total_gb:.0f}GB RAM ({n_containers / total_gb:.1f}/GB). "
            f"Resource pressure may cause widespread failures."
        )


def _profile_user(user_info: dict[str, Any], lines: list[str]) -> None:
    if not user_info.get("puid"):
        return
    puid = user_info["puid"]
    pgid = user_info["pgid"]
    uname = user_info.get("username", "")
    lines.append(f"Server file owner: PUID={puid} PGID={pgid}" + (f" ({uname})" if uname else ""))
    lines.append(
        f"  If logs show 'Permission denied': verify app is configured "
        f"with PUID={puid} PGID={pgid} in .env. Mismatch causes all "
        f"linuxserver.io containers to fail reading their config dirs."
    )


# ── 16b. Data directory disk space ──────────────────────────────────
def _section_data_disk(lines: list[str]) -> None:
    try:
        import shutil as _sh
        from backend.core.config import config as _cfg2

        _du = _sh.disk_usage(str(_cfg2.data_dir))
        _pct = int(_du.used / _du.total * 100)
        _free_gb = (_du.total - _du.used) / 1e9
        if _pct > 80:
            lines.append(
                f"Data dir disk: {_pct}% used ({_free_gb:.1f}GB free) ⚠ "
                + (
                    "CRITICAL — SQLite DB and compose fragments at risk"
                    if _pct > 95
                    else "disk pressure — monitor closely"
                )
            )
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 17. Source availability ─────────────────────────────────────────
def _section_source_availability(db: Any, app_key: str, lines: list[str]) -> None:
    try:
        src = db.execute(
            """SELECT status, error FROM source_availability
               WHERE source_type = 'docker_image' AND resource_key = ?
                 AND status != 'ok' LIMIT 1""",
            (app_key,),
        ).fetchone()
        if src:
            lines.append(f"Docker image: {src['status']} — {src['error'] or 'unavailable'}")
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 18. App category context — diagnosis hints by app type ──────────
_DEBRID_APPS = frozenset({"decypharr", "zilean", "dumb", "cli_debrid", "zurg"})
_ARR_APPS = frozenset(
    {"sonarr", "radarr", "lidarr", "readarr", "whisparr", "prowlarr", "bazarr", "mylar3"}
)
_LLM_APPS = frozenset({"ollama", "localai", "llamacpp_server"})
_PHOTO_APPS = frozenset({"immich"})
_MEDIA_APPS = frozenset({"jellyfin", "plex", "emby"})


def _section_app_category(app_key: str, lines: list[str]) -> None:
    try:
        if app_key in _DEBRID_APPS:
            lines.append(
                "DEBRID APP: This app depends on an external debrid API "
                "(Real-Debrid / TorBox / AllDebrid). Docker-level fixes rarely help. "
                "Check first: (1) is the debrid service itself down? "
                "(2) has the API token expired? "
                "(3) is the rclone mount healthy? "
                "action=manual is correct unless logs show a clear Docker issue."
            )
        elif app_key in _LLM_APPS:
            lines.append(
                "LLM/AI APP: Failures are often model-related, not Docker-related. "
                "Check: (1) does the model fit in available RAM/VRAM? "
                "(2) does the CPU have AVX2? (see CPU info above) "
                "(3) is the model file corrupt? Try: ollama list / ollama pull"
            )
        elif app_key in _ARR_APPS:
            lines.append(
                "ARR APP: Common failure causes: (1) Prowlarr indexer API key expired, "
                "(2) download client unreachable (check Gluetun VPN if used), "
                "(3) database corruption (check logs for 'database disk image is malformed'), "
                "(4) port conflict with another arr app."
            )
        elif app_key in _PHOTO_APPS:
            lines.append(
                "PHOTO/IMMICH APP: Heavily dependent on postgres and Redis (managed services). "
                "If managed services are down, Immich WILL fail — check above. "
                "ML worker failures are usually insufficient RAM for face detection model."
            )
        elif app_key in _MEDIA_APPS:
            lines.append(
                "MEDIA SERVER: Transcoding failures are often GPU/driver issues, not Docker. "
                "Direct play failures suggest network or permission issues on media files."
            )
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 18b. Compose fragment existence ─────────────────────────────────
def _section_compose_fragment(app_key: str, lines: list[str]) -> None:
    try:
        from backend.core.config import config as _cfg

        _frag = _cfg.compose_dir / f"{app_key}.yaml"
        if not _frag.exists():
            lines.append(
                "CRITICAL: No compose fragment — container cannot start. "
                "action=manual. suggested_fix='Go to Catalog → Reinstall. "
                "The stale DB record will be auto-cleared on reinstall.' confidence=0.99"
            )
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 19. ms-test.py results — automated integration test findings ────
def _section_ms_test(app_key: str, lines: list[str]) -> None:
    try:
        import json as _jt
        import pathlib as _pt
        from backend.core.config import config as _cfg

        _test_history = _pt.Path(_cfg.data_dir) / ".." / ".ms-test-history.json"
        if not _test_history.exists():
            _test_history = _pt.Path(__file__).parent.parent.parent / ".ms-test-history.json"
        if not _test_history.exists():
            return
        _th = _jt.loads(_test_history.read_text())
        _runs = _th.get("runs", [])
        if not _runs:
            return
        _last = _runs[-1]
        _pass = _last.get("total_pass", 0)
        _fail = _last.get("total_fail", 0)
        _ts = _last.get("timestamp", "")[:10]
        lines.append(f"ms-test results ({_ts}): {_pass} passed, {_fail} failed")
        if len(_runs) >= 3:
            _fail_names = {f["name"] for f in _runs[-1].get("failures", [])}
            _prev_names = {f["name"] for run in _runs[-3:-1] for f in run.get("failures", [])}
            _repeated = _fail_names & _prev_names
            if _repeated:
                lines.append(
                    f"  Repeated test failures (last 3 runs): {'; '.join(list(_repeated)[:3])}"
                )
        _app_failures = [
            f["name"]
            for f in _last.get("failures", [])
            if app_key and app_key in f.get("detail", "")
        ]
        if _app_failures:
            lines.append(f"  ms-test failures related to {app_key}: {', '.join(_app_failures[:3])}")
        _orphan_fails = [f for f in _last.get("failures", []) if "orphan" in f["name"].lower()]
        if _orphan_fails:
            lines.append(
                "  ⚠ ms-test detected orphaned DB records last run "
                "— startup cleanup may not have run yet"
            )
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 20. Infra app health ────────────────────────────────────────────
def _section_infra_app_health(db: Any, lines: list[str]) -> None:
    try:
        _infra_apps = db.execute(
            "SELECT key, display_name, status, last_healthy_at FROM apps WHERE tier=0 ORDER BY key"
        ).fetchall()
        if not _infra_apps:
            return
        _infra_statuses = []
        for _ia in _infra_apps:
            _age = ""
            if _ia["last_healthy_at"]:
                _mins = int((time.time() - _ia["last_healthy_at"]) / 60)
                _age = f" (last ok {_mins}m ago)" if _mins < 1440 else " (not seen healthy today)"
            _infra_statuses.append(f"{_ia['key']}={_ia['status']}{_age}")
        lines.append("Infra app health: " + ", ".join(_infra_statuses))

        _infra_down = [
            i for i in _infra_apps if i["status"] not in ("running", "installed", "active")
        ]
        if _infra_down:
            lines.append(
                f"⚠ INFRA DEGRADED: {', '.join(i['key'] for i in _infra_down)} "
                f"not running — ALL apps protected by these services will be "
                f"unreachable until infra is restored. Fix infra first."
            )
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 21. ms-update run context ───────────────────────────────────────
def _section_ms_update_context(lines: list[str]) -> None:
    try:
        import json as _ju
        import pathlib as _pu
        from backend.core.config import config as _cfg

        _ctx_path = _pu.Path(_cfg.data_dir) / "last_ms_test_context.json"
        if not _ctx_path.exists():
            return
        _uctx = _ju.loads(_ctx_path.read_text())
        _ran_mins = int((time.time() - _uctx.get("ran_at", 0)) / 60)
        _trigger = _uctx.get("triggered_by", "")
        _days = _uctx.get("days_since_previous", 0)
        if _ran_mins < 1440:  # only show if from today
            lines.append(
                f"Last full test ran {_ran_mins}m ago "
                f"(triggered by: {_trigger}; "
                f"{_days}d gap from previous run)"
            )
        _changed = _uctx.get("code_changed", [])
        if _changed and _ran_mins < 120:
            _api_changes = [f for f in _changed if "backend" in f]
            if _api_changes:
                lines.append(
                    f"Code changed before this test: "
                    f"{', '.join(_api_changes[:5])}" + ("…" if len(_api_changes) > 5 else "")
                )
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)


# ── 22. ms-update recommendation history ────────────────────────────
def _section_ms_update_overdue(lines: list[str]) -> None:
    try:
        import pathlib as _ph
        import time as _th
        from backend.core.config import config as _cfg

        _markers = {
            "trend": _ph.Path(_cfg.data_dir).parent / ".ms-trend-shown",
            "self_improve": _ph.Path(_cfg.data_dir).parent / ".ms-self-improve-shown",
        }
        _label_for = {"trend": "trend", "self_improve": "self-improve"}
        _pending = []
        for _name, _mpath in _markers.items():
            try:
                _age_days = (_th.time() - _mpath.stat().st_mtime) / 86400
                if _age_days >= 14:
                    _pending.append(
                        f"python3 ms-test.py --{_label_for[_name]} ({int(_age_days)}d overdue)"
                    )
            except Exception as e:
                log.debug("diagnostic context section skipped: %s", e)
        if _pending:
            lines.append("Overdue manual commands: " + "; ".join(_pending))
    except Exception as e:
        log.debug("diagnostic context section skipped: %s", e)
