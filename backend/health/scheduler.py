"""backend/health/scheduler.py

Background health check scheduler.

Runs `run_health_cycle()` on a configurable interval (default 30s).
Started automatically by the FastAPI lifespan when the platform is ready.
Stops cleanly on app shutdown via asyncio task cancellation.

Design constraints:
  - Never blocks the API — runs as an asyncio background task
  - Platform must be ready before checks start (wait loop on startup)
  - Each cycle runs all app checks concurrently via asyncio.gather
  - A single failing check never kills the scheduler
  - Interval and agent config are read from settings at each cycle
    so changes take effect without restart
"""

from __future__ import annotations

from typing import Any

from backend.core.logging import get_logger
from backend.health.swallow_counter import record_swallow

import asyncio
import json
import subprocess
import time

log = get_logger(__name__)

# Tracks the running scheduler task so the lifespan can cancel it
_scheduler_task: asyncio.Task[None] | None = None

# Module-level probe failure counters — reset on success, increment on exception
_probe_fail_counts: dict[str, int] = {}

DEFAULT_INTERVAL = 60  # seconds between full check cycles
PLATFORM_READY_POLL = 5  # seconds between platform-ready checks on startup
MAX_STARTUP_WAIT = 300  # give up waiting for platform after 5 minutes


async def _wait_for_platform() -> bool:
    """Wait until the platform is configured and ready.

    Returns True when ready, False if the timeout is exceeded.
    """
    deadline = time.monotonic() + MAX_STARTUP_WAIT
    while time.monotonic() < deadline:
        try:
            from backend.core.state import StateDB

            with StateDB() as db:
                platform = db.get_platform()
            if platform.status == "ready":
                return True
        except Exception:  # noqa: S110  # best-effort platform status check; retry after sleep
            pass
        await asyncio.sleep(PLATFORM_READY_POLL)
    return False


async def _load_cycle_config() -> dict[str, Any]:
    """Load agent and scheduler config from settings DB."""
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            interval_raw = db.get_setting("health_check_interval_secs")
            agent_raw = db.get_setting("llm_agent_config")
            ntfy_topic = db.get_setting("ntfy_topic") or "slop"
            ntfy_url = db.get_setting("ntfy_url") or "http://ntfy:80"
        interval = int(interval_raw) if interval_raw else DEFAULT_INTERVAL
        agent_cfg = json.loads(agent_raw) if agent_raw else {}
        _provider = agent_cfg.get("provider", "ollama")
        if _provider == "llamacpp":
            # Docker container hostname (correct default) — catalog key `llamacpp_server`
            # is the compose container_name; localhost is unreachable from the SLOP container.
            _llm_url = agent_cfg.get("llamacpp_url", "http://llamacpp_server:8081")
        else:
            _llm_url = agent_cfg.get("ollama_url", "http://ollama:11434")
        return {
            "interval": max(30, interval),
            "ollama_url": _llm_url,
            "ntfy_url": ntfy_url,
            "ntfy_topic": ntfy_topic,
        }
    except Exception as _cfg_exc:
        log.error(
            "scheduler config-load failed — falling back to defaults; "
            "LLM diagnosis will be disabled until DB is readable: %s",
            _cfg_exc,
        )
        record_swallow("scheduler.config_load")
        return {
            "interval": DEFAULT_INTERVAL,
            "ollama_url": "",  # provider config unreadable — surface error rather than silently use wrong backend
            "ntfy_url": "http://ntfy:80",
            "ntfy_topic": "slop",
        }


def _set_setting_silently(key: str, value: str) -> None:
    """db.set_setting wrapped in try/except — never raises."""
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            db.set_setting(key, value)
    except Exception:  # intentionally silenced by design — caller expects no-raise
        record_swallow(f"scheduler.set_setting_silently.{key}")


async def _execute_cycle(cfg: dict[str, Any]) -> None:
    """Run one full health cycle and persist its summary."""
    from backend.health.checker import run_health_cycle

    cycle_start = time.monotonic()
    run = await run_health_cycle(
        ollama_url=cfg["ollama_url"],
        ntfy_url=cfg["ntfy_url"],
        ntfy_topic=cfg["ntfy_topic"],
    )
    elapsed = time.monotonic() - cycle_start
    if run.apps_degraded > 0:
        log.warning(
            "Health cycle: %d/%d healthy, %d degraded (%.1fs)",
            run.apps_healthy,
            run.apps_checked,
            run.apps_degraded,
            elapsed,
        )
    else:
        log.debug(
            "Health cycle: %d/%d healthy (%.1fs)",
            run.apps_healthy,
            run.apps_checked,
            elapsed,
        )
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            db.set_setting("health_last_cycle_at", str(int(time.time())))
            db.set_setting(
                "health_last_cycle_summary",
                json.dumps(
                    {
                        "apps_checked": run.apps_checked,
                        "apps_healthy": run.apps_healthy,
                        "apps_degraded": run.apps_degraded,
                        "llm_agent": run.llm_agent_state,
                        "elapsed_ms": int(elapsed * 1000),
                    }
                ),
            )
    except Exception:  # best-effort cycle summary persist; never block the scheduler
        record_swallow("scheduler.cycle_summary_persist")


def _check_docker_daemon_health() -> None:
    """Probe `docker ps` latency; persist a daemon-slow indicator when slow."""
    import time as _t

    _docker_start = _t.monotonic()
    try:
        subprocess.run(
            ["docker", "ps", "--format", "{{.Names}}"],
            capture_output=True,
            timeout=5,
        )
    except Exception as _de:
        log.error("Docker daemon unreachable: %s — skipping health checks", _de)
        return
    _docker_ms = int((_t.monotonic() - _docker_start) * 1000)
    if _docker_ms > 3000:
        log.warning("Docker daemon slow: %dms — all health checks unreliable", _docker_ms)
        _set_setting_silently("docker_daemon_slow_ms", str(_docker_ms))
    else:
        _set_setting_silently("docker_daemon_slow_ms", "0")


def _check_and_restart_traefik() -> None:
    """If Traefik is not running, attempt one compose-up restart."""
    try:
        from backend.core import docker_client as _dc

        _traefik = _dc.get_container("traefik")
        if not _traefik or _traefik.status == "running":
            return
        log.warning(
            "Traefik container is not running (status: %s) — attempting restart", _traefik.status
        )
        from backend.core.config import config as _tc

        _frag = _tc.compose_dir / "traefik.yaml"
        if not _frag.exists():
            return
        result = subprocess.run(
            ["docker", "compose", "-f", str(_frag), "up", "-d", "--quiet-pull"],
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            log.info("Traefik restarted successfully by health scheduler")
        else:
            log.error("Traefik restart failed: %s", result.stderr.decode()[:200])
    except Exception as _te:
        log.debug("Traefik health check failed: %s", _te)


def _check_managed_services_health() -> None:
    """Run check_managed_services and warn on each unhealthy service."""
    try:
        from backend.health.managed_services import check_managed_services

        _ms_results = check_managed_services()
        for _svc, _res in _ms_results.items():
            if not _res["healthy"]:
                log.warning("Managed service '%s' unhealthy: %s", _svc, _res["message"])
    except Exception as _mse:
        log.debug("Managed service check failed: %s", _mse)


def _check_disk_space() -> None:
    """Log a warning when the data dir is over 80% full (error over 95%)."""
    try:
        import shutil as _shu
        from backend.core.config import config as _cfg

        _du = _shu.disk_usage(str(_cfg.data_dir))
        _pct = int(_du.used / _du.total * 100)
        if _pct > 95:
            log.error("Data dir disk CRITICAL: %d%% used", _pct)
        elif _pct > 80:
            log.warning("Data dir disk: %d%% used — low disk space", _pct)
    except Exception:  # noqa: S110  # best-effort disk check; unavailable in some container environments
        pass


def _maybe_start_source_scan() -> None:
    """Schedule a weekly source-availability scan in the background if overdue."""
    try:
        from backend.health.source_checker import due_for_scan, run_source_scan

        if due_for_scan():
            log.info("Source availability scan is due — starting in background.")
            from backend.core.supervisor import spawn_supervised

            spawn_supervised("source-scan", lambda: run_source_scan())
    except Exception as _e:
        log.debug("Source scan check failed: %s", _e)
        record_swallow("scheduler.source_scan_start")


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
        log.warning("auto-apply gate: pre-approval policy unavailable (skipping cycle): %s", imp_err)
        return None

    policy = load_policy()
    out: list[Any] = []
    for row in rows:
        try:
            dc = row["diagnosis_class"] if hasattr(row, "__getitem__") else getattr(row, "diagnosis_class", "")
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

    try:
        from backend.agent.autofix import select_auto_applicable, apply_eligible_fixes
        from backend.agent.types import OperationalLevel

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


def _run_host_probes() -> None:
    """Run host substrate probes. Keeps its own error isolation."""
    try:
        from backend.agent.host_audit import reconcile_host as _reconcile_host
        from backend.agent.spine import persist_findings as _persist_findings
        from backend.core.state import StateDB as _HostStateDB

        _host_findings = _reconcile_host()
        _persist_findings(_host_findings, _HostStateDB)
    except Exception as _host_err:
        log.debug("host substrate probes failed: %s", _host_err)


def _run_recovery_probes() -> None:
    """Run recoverability probes. Keeps its own error isolation."""
    try:
        from backend.agent.recovery_audit import reconcile_recovery as _reconcile_recovery
        from backend.agent.spine import persist_findings as _persist_findings_c
        from backend.core.state import StateDB as _RecovStateDB
        from backend.manifests.loader import load_all_manifests as _load_all_manifests

        _manifests = _load_all_manifests()
        _apps = list(_manifests.values())
        # Resolve the platform config_root so the backup probes can locate
        # <config_root>/backups/<key> for apps that opt into backup.
        _config_root: str | None = None
        try:
            with _RecovStateDB() as _db:
                _config_root = _db.get_platform().config_root
        except Exception as _cr_err:
            log.debug("could not resolve config_root for recovery probes: %s", _cr_err)
        _recovery_findings = _reconcile_recovery(_apps, _config_root)
        _persist_findings_c(_recovery_findings, _RecovStateDB)
    except Exception as _rec_err:
        log.debug("recoverability probes failed: %s", _rec_err)


def _run_cve_probes() -> None:
    """Run CVE remediation probes. Keeps its own error isolation.

    GROUND probe: scans managed app images + SLOP's own image for HIGH/CRITICAL
    CVEs via the bundled trivy scanner and emits health.cve findings. The gated
    auto-heal is evaluated separately by the auto-apply pipeline, not here.
    """
    try:
        from backend.agent.cve_audit import reconcile_cve as _reconcile_cve
        from backend.agent.spine import persist_findings as _persist_findings_cve
        from backend.core.state import StateDB as _CveStateDB
        from backend.manifests.loader import load_all_manifests as _load_manifests_cve

        _cve_apps = list(_load_manifests_cve().values())
        _cve_findings = _reconcile_cve(_cve_apps)
        _persist_findings_cve(_cve_findings, _CveStateDB)
    except Exception as _cve_err:
        log.debug("cve probes failed: %s", _cve_err)


def _run_container_probes() -> None:
    """Run container substrate probes. Keeps its own error isolation."""
    try:
        from backend.agent.container_audit import reconcile_containers as _reconcile_containers
        from backend.agent.spine import persist_findings as _persist_findings_a
        from backend.core.state import StateDB as _ContStateDB

        _container_findings = _reconcile_containers()
        _persist_findings_a(_container_findings, _ContStateDB)
    except Exception as _cont_err:
        log.debug("container substrate probes failed: %s", _cont_err)


def _run_image_probes() -> None:
    """Run image drift probes. Keeps its own error isolation."""
    try:
        from backend.agent.image_audit import reconcile_images as _reconcile_images
        from backend.agent.spine import persist_findings as _persist_findings_b
        from backend.core.state import StateDB as _ImgStateDB
        from backend.manifests.loader import load_all_manifests as _load_manifests_b

        _img_apps = list(_load_manifests_b().values())
        _image_findings = _reconcile_images(_img_apps)
        _persist_findings_b(_image_findings, _ImgStateDB)
    except Exception as _img_err:
        log.debug("image drift probes failed: %s", _img_err)


def _run_scrub_probe() -> None:
    """Run scrub-effectiveness probe. Keeps its own error isolation."""
    try:
        from backend.agent.scrub_probe import check_scrub_effectiveness as _check_scrub
        from backend.agent.spine import persist_findings as _persist_findings_scrub
        from backend.core.state import StateDB as _ScrubStateDB

        _scrub_finding = _check_scrub()
        _persist_findings_scrub([_scrub_finding], _ScrubStateDB)
    except Exception as _scrub_err:
        log.debug("scrub-effectiveness probe failed: %s", _scrub_err)


def _run_smart_probes() -> None:
    """Run SMART / SnapRAID parity probes. Keeps its own error isolation."""
    try:
        from backend.agent.smart_probe import reconcile_smart as _reconcile_smart
        from backend.agent.spine import persist_findings as _persist_findings_smart
        from backend.core.state import StateDB as _SmartStateDB

        _smart_findings = _reconcile_smart()
        _persist_findings_smart(_smart_findings, _SmartStateDB)
    except Exception as _smart_err:
        log.debug("smart/parity probes failed: %s", _smart_err)


def _run_ms_toolkit_probe() -> None:
    """Run the platform self-check toolkit (ms-check --json) as a GROUND probe and
    persist its checks as Findings. Keeps its own error isolation."""
    try:
        from backend.agent.ms_toolkit_audit import reconcile as _reconcile_toolkit
        from backend.agent.spine import persist_findings as _persist_findings_tk
        from backend.core.state import StateDB as _ToolkitStateDB

        _toolkit_findings = _reconcile_toolkit()
        _persist_findings_tk(_toolkit_findings, _ToolkitStateDB)
    except Exception as _tk_err:
        log.debug("platform self-check probe failed: %s", _tk_err)


def _run_pending_wiring() -> None:
    """Retry pending wiring rows on each scheduler cycle (added at A+C merge)."""
    try:
        from backend.manifests.executor import run_pending_wiring as _run_pending_wiring_impl

        _run_pending_wiring_impl()
    except Exception as _wiring_err:
        log.debug("pending wiring retry failed: %s", _wiring_err)


# Ordered names matching the asyncio.gather() call in _scheduler_loop
_PROBE_NAMES = [
    "docker_daemon",
    "traefik",
    "managed_services",
    "disk_space",
    "source_scan",
    "auto_fixes",
    "host_probes",
    "recovery_probes",
    "cve_probes",
    "container_probes",
    "image_probes",
    "scrub_probe",
    "smart_probes",
    "ms_toolkit_probe",
    "pending_wiring",
]


async def _scheduler_loop() -> None:
    """Main scheduler loop. Runs until cancelled.

    Each iteration: load config → (skip if previous still running) → run one
    cycle → run ambient post-cycle checks (docker daemon / Traefik / managed
    services / disk / source scan) → sleep. CancelledError propagates out
    from the sleep or from the in-flight cycle so the FastAPI lifespan can
    cancel cleanly. Other exceptions are logged and the loop continues.
    """
    log.info("Health scheduler: waiting for platform to be ready…")
    if not await _wait_for_platform():
        log.warning(
            "Health scheduler: platform not ready after %ds — "
            "checks will not run until SLOP is restarted.",
            MAX_STARTUP_WAIT,
        )
        return

    log.info("Health scheduler started — platform is ready.")

    cycle_running = False  # non-overlapping guard
    while True:
        cfg = await _load_cycle_config()

        if cycle_running:
            log.debug("Skipping health cycle — previous cycle still running.")
            await asyncio.sleep(cfg["interval"])  # CancelledError propagates
            continue

        cycle_running = True
        try:
            await _execute_cycle(cfg)
        except asyncio.CancelledError:
            cycle_running = False
            raise
        except Exception as e:
            log.error("Health scheduler cycle error: %s", e, exc_info=True)
            # Continue running — a single bad cycle never kills the scheduler
        finally:
            cycle_running = False

        # Ambient post-cycle checks — run all probes concurrently; one failure
        # never cancels others (return_exceptions=True).
        _probe_results = await asyncio.gather(
            asyncio.to_thread(_check_docker_daemon_health),
            asyncio.to_thread(_check_and_restart_traefik),
            asyncio.to_thread(_check_managed_services_health),
            asyncio.to_thread(_check_disk_space),
            asyncio.to_thread(_maybe_start_source_scan),
            asyncio.to_thread(_maybe_auto_apply_safe_fixes),
            asyncio.to_thread(_run_host_probes),
            asyncio.to_thread(_run_recovery_probes),
            asyncio.to_thread(_run_cve_probes),
            asyncio.to_thread(_run_container_probes),
            asyncio.to_thread(_run_image_probes),
            asyncio.to_thread(_run_scrub_probe),
            asyncio.to_thread(_run_smart_probes),
            asyncio.to_thread(_run_ms_toolkit_probe),  # platform self-check toolkit
            asyncio.to_thread(_run_pending_wiring),  # retry deferred wiring rows (C)
            return_exceptions=True,  # one failure never cancels others
        )

        # Track consecutive probe failures; warn and persist at threshold
        for name, result in zip(_PROBE_NAMES, _probe_results, strict=True):
            if isinstance(result, Exception):
                _probe_fail_counts[name] = _probe_fail_counts.get(name, 0) + 1
                count = _probe_fail_counts[name]
                if count >= 5:
                    log.warning(
                        "Probe '%s' has failed %d consecutive times: %s", name, count, result
                    )
                    # Persist to DB
                    try:
                        from backend.core.state import StateDB

                        with StateDB() as db:
                            db.write_probe_failure(name, count, str(result))
                    except (
                        Exception
                    ):  # best-effort DB write; counter stays in memory if DB unavailable
                        record_swallow("scheduler.probe_failure_db_write")
            else:
                _probe_fail_counts.pop(name, None)  # reset on success

        try:
            await asyncio.sleep(cfg["interval"])
        except asyncio.CancelledError:
            log.info("Health scheduler stopping.")
            raise


async def _maybe_run_weekly_summary() -> None:
    """Run a weekly LLM health summary if 7+ days have passed since last one."""
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            last_summary_str = db.get_setting("last_weekly_summary_ts") or "0"
            last_summary = int(last_summary_str)

        import time

        now = int(time.time())
        seven_days = 7 * 24 * 3600
        if now - last_summary < seven_days:
            return  # Not yet time

        log.info("Running weekly health summary…")

        from backend.core.state import StateDB as SDB

        cutoff = now - seven_days

        with SDB() as db:
            rows = db.execute(
                """SELECT subject_key, check_name, status, summary, checked_at
                   FROM health_check_history
                   WHERE checked_at >= ? AND status IN ('error', 'warning')
                   ORDER BY checked_at DESC LIMIT 100""",
                (cutoff,),
            ).fetchall()

        if not rows:
            log.info("No health issues this week — skipping LLM summary.")
            with SDB() as db:
                db.set_setting("last_weekly_summary_ts", str(now))
            return

        # Build summary prompt
        issues_text = "\n".join(
            f"- {r['subject_key']}: {r['check_name']} ({r['status']}) — {r['summary']}"
            for r in rows[:30]
        )
        prompt = (
            f"You are a homelab health assistant. Summarize the following health issues "
            f"from the past 7 days in plain language. Identify patterns, recurring issues, "
            f"and the most important action to take. Be concise — 3-5 sentences max.\n\n"
            f"Issues:\n{issues_text}"
        )

        # Try local LLM
        summary_text = ""
        try:
            from backend.health.checker import _llm_state

            if _llm_state.get("status") == "ready":
                import httpx

                with SDB() as db:
                    _wcfg_raw = db.get_setting("llm_agent_config")
                _wcfg = json.loads(_wcfg_raw) if _wcfg_raw else {}
                _wprovider = _wcfg.get("provider", "ollama")
                if _wprovider == "llamacpp":
                    # Docker container hostname (correct default) — see _load_config above.
                    ollama_url = _wcfg.get("llamacpp_url", "http://llamacpp_server:8081")
                else:
                    ollama_url = _wcfg.get("ollama_url", "http://ollama:11434")
                model = _wcfg.get("ollama_model") or "phi4-mini"
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        f"{ollama_url}/api/generate",
                        json={"model": model, "prompt": prompt, "stream": False},
                    )
                    resp.raise_for_status()
                    summary_text = resp.json().get("response", "")
        except Exception as e:
            log.debug("Weekly summary LLM call failed: %s", e)

        if summary_text:
            with SDB() as db:
                db.set_setting("last_weekly_summary", summary_text[:2000])
                db.set_setting("last_weekly_summary_ts", str(now))
            log.info("Weekly health summary generated (%d chars).", len(summary_text))
        else:
            with SDB() as db:
                db.set_setting("last_weekly_summary_ts", str(now))

    except Exception as e:
        log.debug("Weekly summary failed: %s", e)
        record_swallow("scheduler.weekly_summary_persist")


def start_scheduler() -> None:
    """Start the health scheduler as an asyncio background task.

    Called from FastAPI lifespan. Safe to call multiple times — only
    one scheduler runs at a time.
    """
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        log.debug("Health scheduler already running.")
        return
    from backend.core.supervisor import RestartPolicy, spawn_supervised

    # The scheduler is core infrastructure — auto-restart with backoff so a
    # transient crash does not silently stop all health checking.
    _scheduler_task = spawn_supervised(
        "health-scheduler",
        lambda: _scheduler_loop(),
        restart=RestartPolicy(max_restarts=5),
    )
    log.info("Health scheduler task created.")


def stop_scheduler() -> None:
    """Cancel the scheduler gracefully. Called from FastAPI lifespan shutdown."""
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        _scheduler_task.cancel()
        log.info("Health scheduler cancelled.")
    _scheduler_task = None


def scheduler_status() -> dict[str, Any]:
    """Return current scheduler state (for the health API)."""
    if _scheduler_task is None:
        return {"running": False, "state": "not_started"}
    if _scheduler_task.done():
        exc = _scheduler_task.exception() if not _scheduler_task.cancelled() else None
        return {
            "running": False,
            "state": "stopped",
            "error": str(exc) if exc else None,
        }
    return {
        "running": True,
        "state": _scheduler_task.get_name(),
        "error": None,
    }
