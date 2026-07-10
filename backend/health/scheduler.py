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

from collections.abc import Awaitable, Callable
from typing import Any

from backend.agent.registry import register_probe  # #980: probe self-registration
from backend.core.logging import get_logger
from backend.health import probe_backoff

# Auto-apply governance cluster extracted to scheduler_autoapply.py (#1302 drain).
# Re-imported here so _POST_CYCLE_PROBES and existing callers/tests
# (backend.health.scheduler._maybe_auto_apply_safe_fixes) resolve unchanged; the
# import also fires its @register_probe("auto_fixes") registration.
from backend.health.scheduler_autoapply import _maybe_auto_apply_safe_fixes
from backend.health.swallow_counter import record_swallow

import asyncio
import json
import subprocess
import time

from backend.platform.ollama_runtime import normalize_llm_agent_config

log = get_logger(__name__)

# Tracks the running scheduler task so the lifespan can cancel it
_scheduler_task: asyncio.Task[None] | None = None

DEFAULT_INTERVAL = 60  # seconds between full check cycles
PLATFORM_READY_POLL = 5  # seconds between platform-ready checks on startup
# During the setup wizard the platform may remain pending for several minutes.
# The scheduler should keep polling indefinitely rather than permanently exiting —
# there is no scenario where a permanently dead scheduler is the right outcome.
# Per-probe ceilings for the ambient post-cycle phase (#825). A probe that hangs
# (e.g. a docker SDK call against a wedged daemon with no inner timeout) would
# otherwise block asyncio.gather forever and stall the next cycle. Two budgets,
# because the probes are not uniform:
#   * PROBE_TIMEOUT_SECONDS  -- fast probes (daemon/traefik/disk/wiring) finish in
#     seconds; a tight ceiling cuts a wedged one responsively.
#   * LONG_PROBE_TIMEOUT_SECONDS -- the scan/apply probes (cve, image, source-scan,
#     smart, toolkit, auto-fixes) legitimately run minutes and already cap their
#     OWN inner calls (e.g. trivy at 300s/image over every managed app). A tight
#     outer ceiling would FALSELY fail a healthy-but-slow scan, so theirs is a
#     pure last-resort circuit-breaker, sized far above any realistic real run.
# Both are finite, so an indefinite wedge can no longer stall the scheduler.
PROBE_TIMEOUT_SECONDS = 120
LONG_PROBE_TIMEOUT_SECONDS = 3600


async def _wait_for_platform() -> bool:
    """Wait until the platform is configured and ready.

    Polls indefinitely — the setup wizard may run for several minutes and the
    scheduler should automatically activate as soon as the platform is marked
    ready, without requiring a process restart.
    """
    while True:
        try:
            from backend.core.state import StateDB

            with StateDB() as db:
                platform = db.get_platform()
            if platform.status == "ready":
                return True
        except Exception:  # noqa: S110  # best-effort platform status check; retry after sleep
            pass
        await asyncio.sleep(PLATFORM_READY_POLL)


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
        agent_cfg = normalize_llm_agent_config(json.loads(agent_raw) if agent_raw else {})
        _provider = agent_cfg.get("provider", "ollama")
        if _provider == "llamacpp":
            # Docker container hostname (correct default) — catalog key `llamacpp_server`
            # is the compose container_name; localhost is unreachable from the SLOP container.
            _llm_url = agent_cfg.get("llamacpp_url", "http://llamacpp_server:8081")
        else:
            _llm_url = agent_cfg.get("ollama_url", "http://localhost:11434")
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


@register_probe("docker_daemon", description="Docker daemon reachability/health")
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


@register_probe("traefik", description="Traefik reverse-proxy liveness (platform restart)")
def _check_and_restart_traefik() -> None:
    """If Traefik is not running, attempt one compose-up restart.

    DELIBERATE governance carve-out (#977 F2 — NOT a bypass of
    `agent.governance.authorize`): traefik is SLOP's OWN reverse-proxy ingress, not a
    catalog app. Keeping SLOP itself reachable is the agent's primary continuity duty
    (CLAUDE.md "SLOP AI Agent" — its job is ensuring SLOP runs), a platform-continuity
    regime distinct from autonomous app-remediation. One bounded compose-up attempt;
    platform-critical (no traefik ⇒ SLOP unreachable, including the approval UI a gate
    would route to). This carve-out is the single documented exception to the
    chokepoint invariant guarded by tests/test_agent_authorize_chokepoint.py.
    """
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


@register_probe("managed_services", description="Managed service-unit health")
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


@register_probe("disk_space", description="Host disk-space headroom")
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


@register_probe("source_scan", description="Source/version drift scan (long)")
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


@register_probe("host_probes", description="Host-level reconcilers")
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


@register_probe("recovery_probes", description="Recovery reconcilers")
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


@register_probe("cve_probes", description="CVE audit/auto-heal scan (long)")
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


@register_probe("container_probes", description="Container-state reconcilers")
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


@register_probe("image_probes", description="Image freshness/pin reconcilers (long)")
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


@register_probe("scrub_probe", description="Secret-scrub reconciler")
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


@register_probe("smart_probes", description="SMART disk-health probes (long)")
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


@register_probe("ms_toolkit_probe", description="ms-toolkit health probe (long)")
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


@register_probe("pending_wiring", description="Deferred wiring retry")
def _run_pending_wiring() -> None:
    """Retry pending wiring rows on each scheduler cycle (added at A+C merge)."""
    try:
        from backend.manifests.executor import run_pending_wiring as _run_pending_wiring_impl

        _run_pending_wiring_impl()
    except Exception as _wiring_err:
        log.debug("pending wiring retry failed: %s", _wiring_err)


# Ordered names matching the asyncio.gather() call in _scheduler_loop
# Post-cycle ambient probes, in dispatch order: (name, sync-callable, timeout
# ceiling). Single SSOT — the dispatch loop, the name↔result mapping, and the
# per-probe backoff (#1144) all derive from this one list (no parallel name
# list to drift). Scan/apply probes legitimately run long and cap their own
# inner calls, so they get the generous LONG ceiling; the rest get the tight
# default.
_POST_CYCLE_PROBES: list[tuple[str, Callable[[], Any], float]] = [
    ("docker_daemon", _check_docker_daemon_health, PROBE_TIMEOUT_SECONDS),
    ("traefik", _check_and_restart_traefik, PROBE_TIMEOUT_SECONDS),
    ("managed_services", _check_managed_services_health, PROBE_TIMEOUT_SECONDS),
    ("disk_space", _check_disk_space, PROBE_TIMEOUT_SECONDS),
    ("source_scan", _maybe_start_source_scan, LONG_PROBE_TIMEOUT_SECONDS),
    ("auto_fixes", _maybe_auto_apply_safe_fixes, LONG_PROBE_TIMEOUT_SECONDS),
    ("host_probes", _run_host_probes, PROBE_TIMEOUT_SECONDS),
    ("recovery_probes", _run_recovery_probes, PROBE_TIMEOUT_SECONDS),
    ("cve_probes", _run_cve_probes, LONG_PROBE_TIMEOUT_SECONDS),
    ("container_probes", _run_container_probes, PROBE_TIMEOUT_SECONDS),
    ("image_probes", _run_image_probes, LONG_PROBE_TIMEOUT_SECONDS),
    ("scrub_probe", _run_scrub_probe, PROBE_TIMEOUT_SECONDS),
    ("smart_probes", _run_smart_probes, LONG_PROBE_TIMEOUT_SECONDS),
    ("ms_toolkit_probe", _run_ms_toolkit_probe, LONG_PROBE_TIMEOUT_SECONDS),
    ("pending_wiring", _run_pending_wiring, PROBE_TIMEOUT_SECONDS),
]


def _pt(func: Callable[[], Any], timeout: float = PROBE_TIMEOUT_SECONDS) -> Awaitable[Any]:
    """Run a sync post-cycle probe in a worker thread under a hard timeout (#825).

    A hung probe (e.g. a wedged docker SDK call with no inner timeout) would
    otherwise block `asyncio.gather` indefinitely and stall the post-cycle phase
    plus the next cycle. `asyncio.wait_for` bounds the await; a timeout surfaces
    as `TimeoutError`, which `probe_backoff.record_result` counts as a probe
    failure. Pass `timeout=LONG_PROBE_TIMEOUT_SECONDS` for scan/apply probes
    that legitimately run long, else a healthy slow scan is falsely failed.
    Caveat: asyncio cannot kill the orphaned worker thread (it parks until its
    blocking call returns), but the scheduler is no longer held hostage. A
    chronically-hung probe would still leak one parked thread per cycle — so
    `probe_backoff` SKIPS a probe that has timed out N consecutive cycles for a
    backoff window (#1144, the deferred remainder of #825).
    """
    return asyncio.wait_for(asyncio.to_thread(func), timeout=timeout)


async def _scheduler_loop() -> None:
    """Main scheduler loop. Runs until cancelled.

    Each iteration: load config → (skip if previous still running) → run one
    cycle → run ambient post-cycle checks (docker daemon / Traefik / managed
    services / disk / source scan) → sleep. CancelledError propagates out
    from the sleep or from the in-flight cycle so the FastAPI lifespan can
    cancel cleanly. Other exceptions are logged and the loop continues.
    """
    log.info("Health scheduler: waiting for platform to be ready…")
    await _wait_for_platform()

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
        # never cancels others (return_exceptions=True). Each runs in a worker
        # thread under its per-probe hard timeout (_pt, #825). A probe that has
        # timed out PROBE_BACKOFF_THRESHOLD consecutive cycles is SKIPPED for a
        # backoff window (#1144) — NOT dispatched — so a chronically-hung probe
        # stops leaking one parked worker thread per cycle.
        dispatched: list[tuple[str, Awaitable[Any]]] = []
        backed_off: list[str] = []
        for _name, _func, _timeout in _POST_CYCLE_PROBES:
            if probe_backoff.due_for_dispatch(_name):
                dispatched.append((_name, _pt(_func, _timeout)))
            else:
                backed_off.append(_name)
        if backed_off:
            log.info(
                "Post-cycle probes skipped this cycle (thread-leak backoff #1144): %s",
                ", ".join(backed_off),
            )
        _probe_results = await asyncio.gather(*(aw for _, aw in dispatched), return_exceptions=True)
        # Update per-probe counters + arm/clear the thread-leak backoff; the
        # consecutive-failure warn+persist lives in probe_backoff.record_result.
        for (name, _aw), result in zip(dispatched, _probe_results, strict=True):
            probe_backoff.record_result(name, result)

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
                from backend.core.url_guard_httpx import pinned_async_client

                with SDB() as db:
                    _wcfg_raw = db.get_setting("llm_agent_config")
                _wcfg = normalize_llm_agent_config(json.loads(_wcfg_raw) if _wcfg_raw else {})
                _wprovider = _wcfg.get("provider", "ollama")
                if _wprovider == "llamacpp":
                    # Docker container hostname (correct default) — see _load_config above.
                    ollama_url = _wcfg.get("llamacpp_url", "http://llamacpp_server:8081")
                else:
                    ollama_url = _wcfg.get("ollama_url", "http://localhost:11434")
                model = _wcfg.get("ollama_model") or _wcfg.get("model") or "phi4-mini"
                async with pinned_async_client(timeout=60) as client:
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
    # Clean-slate the per-probe backoff state on a FRESH start (#1144). The
    # supervisor's auto-restarts reuse _scheduler_task and do NOT re-enter here,
    # so a chronic-hang record correctly survives a transient crash; only a
    # genuine (re)start clears it.
    probe_backoff.reset()
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
