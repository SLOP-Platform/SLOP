"""backend/manifests/executor.py

App executor — turns a manifest into a deployed, wired, registered app.

This is the heart of SLOP's automation. It handles the full lifecycle:

  install_app()  — deploy from manifest: deps → compose → start → post-deploy → wire → register
  remove_app()   — clean teardown: stop → unregister → unwire → remove fragment → (optionally) delete config
  replace_app()  — atomic swap: install new → rewire → remove old (used for app-to-app swaps)

All state changes are recorded in the state DB before they happen (optimistic
logging) so interrupted operations can be detected and resumed or rolled back.

Design rules:
  - Every public function returns an ExecutionResult — never raises to callers
  - Plain-language errors only — no raw Docker/subprocess exceptions
  - State DB is always updated even when steps fail (audit trail)
  - Read-only manifests are never mutated
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from backend.infra.base import InfraProvider

import httpx

from backend.core import docker_client
from backend.manifests import db_provision
from backend.core.compose import (
    _SYSTEM_PORTS,
    STACK_NETWORK,
    compose_pull,
    compose_up,
    compose_down,
    build_service_fragment,
    remove_fragment,
    write_compose_file,
    write_fragment,
)
from backend.core.config import config
from backend.core.logging import get_logger
from backend.core.state import PORT_RESERVATION_STALE_MINUTES, StateDB
from backend.manifests.loader import AppManifest, load_manifest
from backend.manifests.seed_config import seed_config_files

# App-to-app wiring pass extracted to executor_wiring.py (#1302 linecount drain).
# Re-exported (redundant-alias idiom) so callers/tests resolve unchanged: apps.py
# + checker.py use run_wiring_pass; scheduler.py + governance.py use
# run_pending_wiring; tests patch executor_wiring._dispatch_wire and call _wire.
from backend.manifests.executor_wiring import (
    run_pending_wiring as run_pending_wiring,
    run_wiring_pass as run_wiring_pass,
    _apply_wire_result as _apply_wire_result,
    _dispatch_wire as _dispatch_wire,
    _reverse_wiring_pass as _reverse_wiring_pass,
    _wire as _wire,
)

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Companion command sanitization (id=723)
# ---------------------------------------------------------------------------
# Denylist approach — matches the existing _SHELL_META_RE pattern used by
# sanitize_manifest() in backend/api/apps.py. Rejects shell metacharacters
# that could enable command injection when the value is passed as Docker's
# `command:` field. Absolute paths are permitted (containers have their own
# namespace); only shell special characters and length abuse are blocked.
_COMPANION_CMD_META_RE = re.compile(r"[;&|`$><\\\n\r()\[\]{}\*\?!#^]")
_COMPANION_CMD_MAX_LEN = 512  # guard against absurdly long injected strings


def _sanitize_companion_command(cmd: str) -> str:
    """Sanitize a companion manifest ``command`` value before it reaches Docker.

    Raises ``ValueError`` when shell metacharacters are present or the value
    exceeds the length cap. Clean values are returned unchanged.
    """
    if not cmd:
        return cmd  # empty string → no command override, pass through
    if len(cmd) > _COMPANION_CMD_MAX_LEN:
        raise ValueError(
            f"companion command too long ({len(cmd)} chars; max {_COMPANION_CMD_MAX_LEN})"
        )
    match = _COMPANION_CMD_META_RE.search(cmd)
    if match:
        raise ValueError(
            f"companion command contains disallowed character {match.group()!r}: {cmd!r}"
        )
    return cmd


# ---------------------------------------------------------------------------
# Concurrent install guard
# ---------------------------------------------------------------------------

import threading as _threading  # noqa: E402  # deferred: install guard must follow module-level constants

_installing: set[str] = set()  # keys currently being installed
_installing_started: dict[str, float] = {}  # key → start timestamp
_install_lock = _threading.Lock()
MAX_INSTALL_SECONDS = 600  # 10 minutes — after this a lock is considered stale

# Invariant (#1100 review #3): a port reservation must stay "live" for at least
# the full install window, else a slow install (large image pull) has its port
# stolen by a concurrent install mid-deploy. Trips at import if either constant
# is changed out of agreement.
assert PORT_RESERVATION_STALE_MINUTES * 60 >= MAX_INSTALL_SECONDS, (
    "PORT_RESERVATION_STALE_MINUTES must cover MAX_INSTALL_SECONDS"
)


# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------


@dataclass
class StepLog:
    name: str
    status: str  # ok | error | skipped | warning
    message: str
    detail: str = ""


# ---------------------------------------------------------------------------
# Step broadcaster — writes each step to DB for real-time polling
# ---------------------------------------------------------------------------


def _broadcast_step_to_db(op_key: str, step: StepLog) -> None:
    """Persist a step to the DB immediately so the UI can poll it."""
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            db.write_op_step(
                op_key=op_key,
                step_name=step.name,
                status=step.status,
                message=step.message,
                detail=step.detail or "",
            )
    except Exception as e:
        log.debug("Could not persist step to DB: %s", e)


def _broadcast_step_to_agent(app_key: str, step: StepLog) -> None:
    """Fire-and-forget: hand error steps to the LLM agent listener (Phase A).

    Uses asyncio.ensure_future so the install pipeline is never blocked.
    If there is no running event loop (e.g. CLI invocation), the exception
    is caught and logged at DEBUG level — never propagated.
    """
    try:
        from backend.agent.listener import install_failure_listener

        _listener_task = asyncio.ensure_future(  # noqa: RUF006  # fire-and-forget; kept alive by event loop
            install_failure_listener(app_key, step.__dict__)
        )
    except Exception as _e:
        log.debug("Could not fire install_failure_listener for %s: %s", app_key, _e)


@dataclass
class ExecutionResult:
    ok: bool
    app_key: str
    operation: str  # install | remove | replace
    steps: list[StepLog] = field(default_factory=list)
    error: str = ""  # plain-language summary of what went wrong

    def add(self, name: str, status: str, message: str, detail: str = "") -> None:
        step = StepLog(name, status, message, detail)
        self.steps.append(step)
        if status == "error" and not self.error:
            self.error = message
        # Write to DB immediately so the progress polling endpoint sees it in real-time
        _broadcast_step_to_db(self.app_key, step)
        # Fire-and-forget: notify the LLM agent listener on error steps (Phase A)
        if status == "error":
            _broadcast_step_to_agent(self.app_key, step)

    def fail(self, name: str, message: str, detail: str = "") -> ExecutionResult:
        self.add(name, "error", message, detail)
        self.ok = False
        return self


# ---------------------------------------------------------------------------
# Install
# ---------------------------------------------------------------------------


def _install_clear_stale_locks() -> None:
    """Clear locks from prior installs that timed out.
    A lock older than MAX_INSTALL_SECONDS means the worker thread is gone."""
    _now = time.time()
    stale = [k for k, t in list(_installing_started.items()) if _now - t > MAX_INSTALL_SECONDS]
    for _sk in stale:
        _installing.discard(_sk)
        _installing_started.pop(_sk, None)
        log.warning("Cleared stale install lock for '%s' (> %ds)", _sk, MAX_INSTALL_SECONDS)


def _install_load_manifest(key: str, result: ExecutionResult) -> AppManifest | None:
    """Load the app's manifest. On failure, records the error on `result`
    and returns None — caller should `return result` immediately."""
    try:
        return load_manifest(key)
    except KeyError:
        result.fail("load_manifest", f"No app '{key}' found in catalog.")
        return None
    except Exception as e:
        result.fail("load_manifest", f"Could not load manifest for '{key}'.", str(e))
        return None


def _install_clear_stale_failed_record(key: str) -> None:
    """Reinstall safety: if app exists as 'failed' with no compose fragment,
    drop the stale DB row so the install starts clean. Best-effort —
    failures are silenced.

    Uses an inline `from backend.core.config import config` import to
    pick up test-fixture monkeypatches of `cfg_mod.config` (e.g.
    test_integration.py's `tmp_env`). A module-top `config` reference
    is captured at import time and would miss the patch.
    """
    try:
        from backend.core.config import config as _cfg

        with StateDB() as _db:
            _existing = _db.get_app(key)
            if _existing and _existing.status == "failed":
                _frag = _cfg.compose_dir / f"{key}.yaml"
                if not _frag.exists():
                    _db.execute("DELETE FROM apps WHERE key=?", (key,))
                    log.info(
                        "Cleared stale failed record for '%s' — clean reinstall",
                        key,
                    )
    except Exception:  # noqa: S110  # best-effort stale record cleanup; install proceeds regardless
        pass


def _install_register_cf_hostname(key: str, manifest: AppManifest, result: ExecutionResult) -> None:
    """Register a CF hostname for the just-installed app. Non-fatal —
    a registration failure is recorded as a warning step but does not
    flip result.ok."""
    try:
        with StateDB() as db:
            platform = db.get_platform()
        hostname_step = _register_app_hostname(key, manifest, platform)
        result.steps.append(hostname_step)
    except Exception as e:
        result.steps.append(
            StepLog(
                name="hostname_register",
                status="warning",
                message="Hostname registration skipped due to an error.",
                detail=str(e),
            )
        )


def _install_autoconfig_ollama() -> None:
    """When ollama is the just-installed app, auto-set the LLM agent URL.
    Ollama runs as a container on the slop network, so the correct
    URL from inside the slop container is http://ollama:11434.
    Best-effort — failures are silenced (logged at debug)."""
    try:
        with StateDB() as _db2:
            _cfg2 = json.loads(_db2.get_setting("llm_agent_config") or "{}")
            _cfg2.setdefault("provider", "ollama")
            _cfg2["ollama_url"] = "http://ollama:11434"
            _db2.set_setting("llm_agent_config", json.dumps(_cfg2))
        log.info("Auto-configured Ollama URL: http://ollama:11434")
    except Exception as _e2:
        log.debug("Could not auto-configure Ollama URL: %s", _e2)


def _install_cleanup_failed_record(key: str, db: StateDB) -> None:
    """Failed install — clean up the DB record for safe retry.
    Compose fragment exists → mark 'failed'; missing → delete row.

    Inline import (see `_install_clear_stale_failed_record`) so test
    fixtures that monkeypatch `backend.core.config.config` are
    picked up at call time.
    """
    try:
        from backend.core.config import config as _cfg

        _frag = _cfg.compose_dir / f"{key}.yaml"
        if _frag.exists():
            db.upsert_app(key, status="failed")
        else:
            db.execute("DELETE FROM apps WHERE key=?", (key,))
            log.info("Install of '%s' failed before deploy — DB record removed", key)
    except Exception:  # noqa: S110  # best-effort DB status update on failed install; never fatal
        pass


def _install_finalize(op_id: int | None, key: str, result: ExecutionResult) -> None:
    """Close out the install: complete operation log, upsert app row,
    auto-config side-effects on success, cleanup on failure."""
    if op_id is None:
        return
    with StateDB() as db:
        db.complete_operation(
            op_id,
            status="completed" if result.ok else "failed",
            error=result.error or None,
        )
        if result.ok:
            db.upsert_app(
                key,
                status="running",
                last_healthy_at=int(time.time()),
            )
            if key == "ollama":
                _install_autoconfig_ollama()
        else:
            _install_cleanup_failed_record(key, db)


def install_app(
    key: str,
    extra_env: dict[str, str] | None = None,
    host_port_override: int | None = None,
    user_volume_paths: dict[str, str] | None = None,
) -> ExecutionResult:
    """Install an app from its catalog manifest.

    Steps:
      1. validate   — platform ready, app not already installed
      2. deps       — auto-deploy PostgreSQL / Redis / companions if needed
      3. config_dir — create the config directory on the host
      4. fragment   — build and write the compose fragment
      5. deploy     — docker compose up
      6. post_deploy — wait_healthy, api_ready, wire steps
      7. register   — record in state DB, log operation

    user_volume_paths: maps install_prompt keys to user-supplied paths
    (id=816). Passed through to the compose builder for <prompt:{key}>
    sentinel substitution in custom volume host_paths.

    Step 2.7.c: orchestrator delegates pre/post phases to helpers
    (`_install_clear_stale_locks`, `_install_load_manifest`,
    `_install_clear_stale_failed_record`, `_install_register_cf_hostname`,
    `_install_finalize`) — drops complexity from 18 to ≤ 5.
    """
    # Step 4.1 wire-up: time the full install pipeline. Outcome label
    # is success/failed; the histogram covers the 1s-10min range.
    from backend.core.metrics import install_duration_seconds

    _install_t0 = time.monotonic()

    result = ExecutionResult(ok=True, app_key=key, operation="install")
    _install_clear_stale_locks()

    manifest = _install_load_manifest(key, result)
    if manifest is None:
        install_duration_seconds.labels(
            app_key=key,
            outcome="failed",
        ).observe(time.monotonic() - _install_t0)
        return result
    _install_clear_stale_failed_record(key)

    with _install_lock:
        _installing.add(key)
        _installing_started[key] = time.time()
    with StateDB() as db:
        op_id = db.log_operation(
            "install",
            "app",
            key,
            detail={"manifest_hash": manifest.content_hash},
        )

    try:
        _install_inner(manifest, result, extra_env, host_port_override, user_volume_paths)
    except Exception as e:
        result.fail("unexpected", "An unexpected error occurred during install.", str(e))
        log.exception("Unexpected error installing %s", key)

    if result.ok:
        _install_register_cf_hostname(key, manifest, result)
        api_ready_ok = any(s.name == "post_api_ready" and s.status == "ok" for s in result.steps)
        result.steps.append(_run_smoke_test(key, manifest, api_ready_passed=api_ready_ok))

    _install_finalize(op_id, key, result)
    install_duration_seconds.labels(
        app_key=key,
        outcome="success" if result.ok else "failed",
    ).observe(time.monotonic() - _install_t0)
    return result


# Apps with internal ports that conflict with Traefik (80/443) or SLOP
# (8080/8081) cannot bind a host port — they are only reachable via Traefik
# hostname routing. The reserved set is single-sourced in backend.core.compose
# (_SYSTEM_PORTS, imported above).


def _validate_install(
    platform: Any, manifest: AppManifest, key: str, existing: Any, result: ExecutionResult
) -> bool:
    """Validate step. Returns True to continue, False to stop.

    The 'already running and healthy' early-success path sets result.ok=True
    and returns False — caller must abort because the work is already done.
    """
    if platform.status != "ready":
        result.fail(
            "validate",
            "Platform setup is not complete.",
            "Run the platform wizard before installing apps.",
        )
        return False

    if existing and existing.status == "running":
        # Verify the container is actually healthy before blocking retry —
        # the health scheduler may have updated status while the user was
        # reading the error.
        try:
            _container = docker_client.get_container(existing.container_name or key)
        except Exception:
            _container = None
        if (
            _container
            and _container.health in ("healthy", "none")
            and _container.status == "running"
        ):
            result.add("validate", "ok", f"{manifest.display_name} is already running and healthy.")
            result.ok = True
            return False  # success early-return
        if _container:
            log.info(
                "Allowing retry of '%s' — container unhealthy: %s/%s",
                key,
                _container.status,
                _container.health,
            )

    result.add("validate", "ok", "Validation passed.")
    return True


def _provision_app_db(
    engine: str, manifest: AppManifest, result: ExecutionResult, provisioned_default: str
) -> None:
    """#1210: idempotently create the per-app managed-DB the app targets (no-op when it targets the
    provisioned default or declares no target). Records a step only when something was attempted."""
    r = db_provision.ensure_app_database(
        engine, manifest.env, provisioned_default=provisioned_default
    )
    if r.status != "skipped":
        result.add(f"deps_{engine}_db", r.status, r.message, r.detail)


def _install_dependencies(manifest: AppManifest, platform: Any, result: ExecutionResult) -> bool:
    """Resolve postgres / redis / mariadb / app / companion deps. Returns True to continue."""
    deps = manifest.dependencies

    if deps.postgres:
        pr = _ensure_managed_service("postgres", network_name=platform.network_name)
        # "warning" (not "error") so the UI surfaces this as a wait, not failure
        step_status = pr["status"] if pr["status"] != "error" else "warning"
        result.add(
            "deps_postgres",
            step_status,
            pr["message"] if pr["status"] != "error" else f"Waiting for postgres… {pr['message']}",
            pr.get("detail", ""),
        )
        if pr["status"] == "error":
            return False
        # #1210 defect-2: the managed postgres provisions only its default DB (`slop`); apps that
        # connect to a per-app DB (affine→affine, …) need it provisioned or their migrations run
        # against a non-existent database (broken install). SERVER-VERIFY gated (db_provision).
        _provision_app_db("postgres", manifest, result, "slop")

    if deps.redis:
        rr = _ensure_managed_service("redis", network_name=platform.network_name)
        step_status = rr["status"] if rr["status"] != "error" else "warning"
        result.add(
            "deps_redis",
            step_status,
            rr["message"] if rr["status"] != "error" else f"Waiting for redis… {rr['message']}",
            rr.get("detail", ""),
        )
        if rr["status"] == "error":
            return False

    if deps.mariadb:
        mr = _ensure_managed_service("mariadb", network_name=platform.network_name)
        step_status = mr["status"] if mr["status"] != "error" else "warning"
        result.add(
            "deps_mariadb",
            step_status,
            mr["message"] if mr["status"] != "error" else f"Waiting for mariadb… {mr['message']}",
            mr.get("detail", ""),
        )
        if mr["status"] == "error":
            return False
        # #1210 blast-radius: same per-app DB need for mariadb (default provisioned DB `booklore`).
        _provision_app_db("mariadb", manifest, result, "booklore")

    for dep_key in deps.apps:
        with StateDB() as db:
            dep = db.get_app(dep_key)
        if dep is None:
            result.add(
                "deps_app",
                "warning",
                f"'{dep_key}' is not installed — wiring will be deferred.",
                f"Install '{dep_key}' first if you need automatic wiring, or configure manually.",
            )
            # continue — do not return False

    if manifest.companions:
        cr = _deploy_companions(manifest, platform)
        result.add("companions", cr["status"], cr["message"], cr.get("detail", ""))
        if cr["status"] == "error":
            return False

    if deps.postgres or deps.redis or deps.mariadb or manifest.companions:
        result.add("deps", "ok", "All dependencies are ready.")
    else:
        result.add("deps", "skipped", "No dependencies required.")
    return True


def _generate_auto_secrets(
    manifest: AppManifest,
    extra_env: dict[str, str] | None,
) -> dict[str, str]:
    """Generate and persist any auto_secrets declared in the manifest.

    For each {key, length} entry in manifest.auto_secrets: if the key is not
    already in .env, generate a random hex value of `length` bytes and write
    it to .env. Returns the (possibly new) extra_env dict with generated values
    so they are also passed as env_overrides to the compose builder.
    """
    if not manifest.auto_secrets:
        return extra_env or {}

    import secrets as _secrets

    from backend.core.config import config as _cfg

    env_path = _cfg.env_file

    existing: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()

    generated: dict[str, str] = {}
    for entry in manifest.auto_secrets:
        key_name = entry.get("key", "")
        length = int(entry.get("length", 32))
        if not key_name:
            continue
        if key_name not in existing and (extra_env is None or key_name not in extra_env):
            generated[key_name] = _secrets.token_hex(length)

    if generated:
        existing.update(generated)
        content = "\n".join(f"{k}={v}" for k, v in sorted(existing.items())) + "\n"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(content)
        import os as _os

        _os.chmod(env_path, 0o600)
        log.info("auto_secrets: generated %s for %s", list(generated), manifest.key)

    result_env = dict(extra_env or {})
    result_env.update(generated)
    return result_env


def _ensure_env_secrets(keys: dict[str, int]) -> None:
    """Generate-if-absent managed secrets into the shared .env (mode 0600).

    ``keys`` maps an env var name → token byte-length. Existing values are left
    untouched (idempotent: the secret is generated once, on first provision, and
    reused on every subsequent install). Used by managed services that must have a
    real credential in .env before their container first starts (#1203) — distinct
    from manifest ``auto_secrets``, which run AFTER dependency provisioning.
    """
    import secrets as _secrets

    from backend.core.config import config as _cfg

    env_path = _cfg.env_file
    existing: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                existing[k.strip()] = v.strip()

    generated: dict[str, str] = {}
    for key_name, length in keys.items():
        if key_name not in existing or not existing[key_name]:
            generated[key_name] = _secrets.token_hex(int(length))

    if generated:
        existing.update(generated)
        content = "\n".join(f"{k}={v}" for k, v in sorted(existing.items())) + "\n"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(content)
        os.chmod(env_path, 0o600)
        log.info("managed-secret: generated %s", list(generated))


def _ensure_config_dir(
    platform: Any, key: str, result: ExecutionResult
) -> tuple[Path, bool] | None:
    """Create the per-app config dir. Returns (path, created_now) or None on error.

    `created_now` is True when this call created the directory (used by the
    deploy step to know whether to clean it up on a failed deploy).
    """
    config_path = Path(platform.config_root) / key
    created_now = not config_path.exists()
    try:
        config_path.mkdir(parents=True, exist_ok=True)
        os.chmod(config_path, 0o755)  # noqa: S103  # nosec B103  # container UID maps to PUID; 0o755 required for service access
        try:
            os.chown(config_path, platform.puid, platform.pgid)
        except OSError:
            pass  # non-root service; container handles ownership internally
        result.add("config_dir", "ok", f"Config directory ready: {config_path}")
    except OSError as e:
        result.fail(
            "config_dir",
            f"Could not create config directory at {config_path}.",
            f"Check that the user running SLOP can write to {platform.config_root}. Error: {e}",
        )
        return None
    return config_path, created_now


# F6b: seeding logic lives in seed_config.py (keeps executor under its size cap).
def _seed_config_files(manifest: AppManifest, config_path: Path, result: ExecutionResult) -> bool:
    """Write manifest.seed_config starter files into config_path before deploy."""
    return seed_config_files(manifest, config_path, result)


def _compute_host_port(manifest: AppManifest, host_port_override: int | None) -> int | None:
    """Resolve the externally-bindable host port.

    Fast path: raw_port not in _SYSTEM_PORTS and not taken → return raw_port.
    Remap path: when raw_port conflicts (system port or given override conflicts),
    find the next free host port above raw_port by checking DB-recorded ports,
    live container ports, and active port_reservations, skipping any candidate
    that is still in _SYSTEM_PORTS.

    Active (non-stale, <5min) reservations held by OTHER apps are part of `taken`
    (#1099): _check_port_conflict rejects a reserved port, so remapping onto one
    would compute a port that then immediately hard-fails the conflict check. The
    app's own reservation is excluded (mirrors _check_port_conflict's `key != ?`).
    """
    raw_port = host_port_override or manifest.web_port
    if raw_port is None:
        return None
    # Collect taken host ports from the DB (active/installed apps) + active reservations.
    with StateDB() as _hpdb:
        _rows = _hpdb.execute(
            "SELECT host_port FROM apps WHERE host_port IS NOT NULL"
            " AND status NOT IN ('failed','removing')",
        ).fetchall()
        _res_rows = _hpdb.execute(
            """SELECT port FROM port_reservations
               WHERE key != ?
               AND (julianday('now') - julianday(reserved_at)) * 1440 < ?""",
            (manifest.key, PORT_RESERVATION_STALE_MINUTES),
        ).fetchall()
    db_ports: set[int] = {int(r[0]) for r in _rows}
    reserved_ports: set[int] = {int(r[0]) for r in _res_rows}
    running_ports: set[int] = set(docker_client.ports_in_use().keys())
    taken = _SYSTEM_PORTS | db_ports | running_ports | reserved_ports
    if raw_port not in taken:
        return raw_port
    # Need to remap: find next candidate above raw_port not in taken.
    candidate = raw_port + 1
    while candidate in taken:
        candidate += 1
    return candidate


def _check_port_conflict(key: str, host_port: int | None, result: ExecutionResult) -> bool:
    """Return False (and result.fail) when host_port is held by another app.

    Checks (in order):
    1. Running containers (docker_client.ports_in_use)
    2. DB app records — a stopped app (status='installed') still owns its port
    3. port_reservations table — a replace_app in progress holds a DB-level
       reservation before stopping the old container, preventing TOCTOU races.
       Reservations older than 5 minutes are considered stale and are ignored.
    """
    if host_port is None:
        return True
    in_use = docker_client.ports_in_use()
    if host_port in in_use and in_use[host_port] != key:
        result.fail(
            "port_check",
            f"Port {host_port} is already in use by running container '{in_use[host_port]}'.",
            "Choose a different port or stop the conflicting container first.",
        )
        return False
    with StateDB() as _pdb:
        _db_owner = _pdb.execute(
            "SELECT key FROM apps WHERE host_port=? AND key!=? "
            "AND status NOT IN ('disabled','failed','removing')",
            (host_port, key),
        ).fetchone()
    if _db_owner:
        result.fail(
            "port_check",
            f"Port {host_port} is reserved by installed app '{_db_owner[0]}' (may be stopped).",
            f"Start '{_db_owner[0]}' to confirm it still needs that port, or remove it and retry.",
        )
        return False
    # Check DB-level port reservation (TOCTOU guard set by replace_app /
    # install_app). Reservations older than PORT_RESERVATION_STALE_MINUTES are
    # treated as stale (crash leftovers) — see that constant for why it tracks
    # MAX_INSTALL_SECONDS (#1100 review #3).
    with StateDB() as _pdb:
        _res_owner = _pdb.execute(
            """SELECT key FROM port_reservations
               WHERE port = ? AND key != ?
               AND (julianday('now') - julianday(reserved_at)) * 1440 < ?""",
            (host_port, key, PORT_RESERVATION_STALE_MINUTES),
        ).fetchone()
    if _res_owner:
        result.fail(
            "port_check",
            f"Port {host_port} is reserved by an in-progress operation for "
            f"'{_res_owner[0]}'. Wait for that operation to complete and retry.",
        )
        return False
    return True


def _build_compose_service(
    manifest: AppManifest,
    key: str,
    platform: Any,
    host_port: int | None,
    config_path: Path,
    extra_env: dict[str, str] | None,
    user_volume_paths: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Build the docker compose service-fragment dict (no I/O).

    user_volume_paths: maps install_prompt keys to user-supplied paths.
    Custom volumes whose host_path is a '<prompt:{key}>' sentinel are
    resolved from this dict; missing keys fall back to the prompt default or
    are skipped with a warning (id=816).
    """
    with StateDB() as db:
        slot_auth = db.get_slot("auth")
        slot_tunnel = db.get_slot("tunnel")  # noqa: F841 — held for parity with original

    tinyauth_enabled = slot_auth.provider == "tinyauth" and slot_auth.status == "active"
    lan_subnet = platform.config.get("lan_subnet") if hasattr(platform, "config") else None

    # Build a lookup of prompt defaults from the manifest for fallback resolution.
    # getattr guards against test stubs that use SimpleNamespace without install_prompts.
    _prompt_defaults: dict[str, str] = {
        p["key"]: p.get("default", "")
        for p in (getattr(manifest, "install_prompts", None) or [])
        if isinstance(p, dict) and p.get("key")
    }
    _uvp = user_volume_paths or {}

    extra_vols = []
    for v in manifest.custom_volumes:
        if v.prompt_key:
            # Resolve the user-supplied path; fall back to default; skip if empty.
            resolved = _uvp.get(v.prompt_key) or _prompt_defaults.get(v.prompt_key, "")
            if not resolved:
                log.warning(
                    "manifest %s: install_prompt key %r not provided and has no default"
                    " — skipping bind mount for container path %s",
                    manifest.key,
                    v.prompt_key,
                    v.container_path,
                )
                continue
            extra_vols.append(
                {
                    "host": resolved,
                    "container": v.container_path,
                    "readonly": v.readonly,
                }
            )
        else:
            extra_vols.append(
                {
                    "host": _expand_path(v.host_path, platform),
                    "container": v.container_path,
                    "readonly": v.readonly,
                }
            )
    service_fragment = build_service_fragment(
        manifest_key=key,
        display_name=manifest.display_name,
        image=manifest.image,
        image_tag=manifest.image_tag,
        web_port=manifest.web_port,
        host_port=host_port,
        config_path=str(config_path),
        config_volume=manifest.config_volume,
        media_root=platform.media_root if manifest.media_volume else None,
        media_volume=manifest.media_volume or "/data",
        domain=platform.domain or "localhost",
        network_name=platform.network_name,
        puid=platform.puid,
        pgid=platform.pgid,
        timezone=platform.timezone,
        linuxserver=manifest.linuxserver,
        env_overrides=extra_env,
        static_env=manifest.env or {},
        cert_resolver=platform.cert_resolver,
        lan_subnet=lan_subnet,
        tinyauth_enabled=tinyauth_enabled,
        extra_volumes=extra_vols,
    )
    _EXTRA_CONFIG_BLOCKLIST = frozenset(
        {
            "privileged",
            "network_mode",
            "pid",
            "userns_mode",
            "user",
            "ipc",
            "cap_add",
            "cap_drop",
            "security_opt",
        }
    )
    if manifest.extra_config:
        safe_extra = {
            k: v for k, v in manifest.extra_config.items() if k not in _EXTRA_CONFIG_BLOCKLIST
        }
        for _blocked in set(manifest.extra_config) - set(safe_extra):
            log.warning("manifest %s: blocked extra_config key '%s'", manifest.key, _blocked)
        service_fragment.update(safe_extra)
    return service_fragment


def _write_compose_files(
    key: str, service_fragment: dict[str, Any], result: ExecutionResult
) -> Path | None:
    """Write the per-app fragment + the parent docker-compose.yml.
    Returns the fragment path on success, None on OSError."""
    try:
        frag_path = write_fragment(key, service_fragment)
        write_compose_file(config.compose_dir / "docker-compose.yml")
        result.add("fragment", "ok", f"Compose fragment written: {frag_path.name}")
        return frag_path
    except OSError as e:
        result.fail("fragment", "Could not write compose fragment.", str(e))
        return None


def _run_deploy(
    key: str,
    frag_path: Path,
    config_path: Path,
    dir_created_now: bool,
    result: ExecutionResult,
    manifest: AppManifest | None = None,
) -> bool:
    """`docker compose up` for the app's fragment. Cleans up on failure.

    Returns True when the container started successfully. On failure, removes
    the fragment and (if we created it this run) the config dir, then returns
    False so the caller can abort.
    """
    # pull_timeout_s defaults to 600 when manifest is not available (defensive fallback)
    pull_timeout: int = manifest.pull_timeout_s if manifest is not None else 600
    deploy_result = _docker_compose_up(key, frag_path, pull_timeout)
    if deploy_result["status"] == "error":
        result.fail("deploy", deploy_result["message"], deploy_result.get("detail", ""))
        remove_fragment(key)
        if dir_created_now and config_path.exists():
            try:
                shutil.rmtree(config_path)
                log.info("Cleaned up config_dir '%s' after failed deploy", config_path)
            except Exception as _ce:
                log.debug("Could not clean config_dir '%s': %s", config_path, _ce)
        return False
    return True


# wait_healthy and api_ready are deferred to the health scheduler —
# these steps blocked install threads for 60-180 s per app while the
# scheduler would have handled them anyway on its normal cadence.
_DEFERRED_STEP_TYPES: frozenset[str] = frozenset({"wait_healthy", "api_ready"})


def _run_post_deploy_steps(manifest: AppManifest, platform: Any, result: ExecutionResult) -> None:
    """Walk manifest.post_deploy. On error, mark result not-ok but don't roll back —
    the container is running; the user can retry wiring manually.

    wait_healthy and api_ready are skipped inline — the health scheduler
    handles them on its normal cadence without blocking the install thread."""
    for step in manifest.post_deploy:
        if step.step_type in _DEFERRED_STEP_TYPES:
            result.add(
                f"post_{step.step_type}",
                "skipped",
                "Health verification deferred to health scheduler.",
            )
            continue
        sr = _run_post_deploy_step(step, manifest, platform)
        result.add(f"post_{step.step_type}", sr["status"], sr["message"], sr.get("detail", ""))
        if sr["status"] == "error":
            result.ok = False
            result.error = sr["message"]
            break


def _register_install(
    manifest: AppManifest,
    key: str,
    host_port: int | None,
    config_path: Path,
    extra_env: dict[str, str] | None,
    result: ExecutionResult,
) -> None:
    """Final upsert into the apps table — records the successful install."""
    with StateDB() as db:
        db.upsert_app(
            key,
            display_name=manifest.display_name,
            tier=manifest.tier,
            category=manifest.category,
            status="running",
            image=manifest.image,
            image_tag=manifest.image_tag,
            container_name=key,
            web_port=manifest.web_port,
            host_port=host_port,
            config_path=str(config_path),
            manifest_source="catalog",
            manifest_hash=manifest.content_hash,
            extra_config=extra_env,
        )
    result.add("register", "ok", f"{manifest.display_name} installed and registered.")


# Volume host-paths that are never directories we should create.
_COMMUNITY_VOL_SKIP_PREFIXES = ("/var/run/", "/dev/", "/proc/", "/sys/")


def _community_volume_host_path(vol: Any, config_root: str, media_root: str) -> str | None:
    """Resolve a single compose-volume entry to an absolute, creatable host path.

    Returns None when the entry is a named volume, a relative path, a
    socket/device/proc path, or a path that already exists as a non-directory.
    """
    # Extract host path from string "host:container[:opts]" or dict format
    if isinstance(vol, str) and ":" in vol:
        host_raw = vol.split(":")[0].strip()
    elif isinstance(vol, dict):
        host_raw = str(vol.get("source") or "").strip()
    else:
        return None

    # Expand Docker Compose var references used in custom compose YAMLs
    host = host_raw.replace("${CONFIG_ROOT}", config_root)
    host = host.replace("${MEDIA_ROOT}", media_root)

    # Only handle absolute paths — relative paths are not portable
    if not host.startswith("/"):
        return None
    # Skip socket/device/proc paths — not directories
    if any(host.startswith(p) for p in _COMMUNITY_VOL_SKIP_PREFIXES):
        return None
    # Skip paths that exist as non-directory (socket, device, etc.)
    if os.path.exists(host) and not os.path.isdir(host):
        return None
    return host


def _ensure_community_volume_dir(host: str, puid: int, pgid: int, result: ExecutionResult) -> None:
    """Create + chown one community bind-mount directory. Failures are warnings."""
    try:
        if not os.path.exists(host):
            os.makedirs(host, exist_ok=True)
            os.chown(host, puid, pgid)
            os.chmod(host, 0o755)  # noqa: S103  # nosec B103  # container UID maps to PUID; 0o755 required for service access
            result.add("config_dir", "ok", "Custom volume directory created: " + host)
        else:
            # Directory already exists — ensure ownership
            os.chown(host, puid, pgid)
    except OSError as exc:
        log.warning("Could not create custom volume dir %s: %s", host, exc)


def _load_community_compose_services(key: str) -> dict[str, Any] | None:
    """Load and normalise the community compose YAML's services map.

    Returns the services dict, or None when there is no community compose file
    or it cannot be parsed into a usable services mapping.
    """
    import yaml as _yaml
    from backend.core.config import config as _cfg

    compose_path = _cfg.catalog_dir / "community" / (key + ".compose.yaml")
    if not compose_path.exists():
        return None

    try:
        doc = _yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("Could not parse community compose YAML for %s: %s", key, exc)
        return None

    if not isinstance(doc, dict):
        return None

    services = doc.get("services") or {}
    if not services and "image" in doc:
        services = {"app": doc}
    return services


def _ensure_community_compose_dirs(
    key: str,
    platform: Any,
    result: ExecutionResult,
) -> None:
    """Create host directories for bind-mount volumes in a community (custom) compose YAML.

    Called after _ensure_config_dir() for community app installs.  Reads the
    raw compose YAML saved at catalog/community/<key>.compose.yaml, resolves
    ${CONFIG_ROOT} / ${MEDIA_ROOT} references, and creates + chowns any missing
    host directories before 'docker compose up'.

    Skips: named volumes, socket/device paths, relative paths, paths that are
    already non-directory files. Failures are warnings — they do not block the
    install so a permission error on one extra directory does not abort the deploy.
    """
    services = _load_community_compose_services(key)
    if not services:
        return

    config_root = str(getattr(platform, "config_root", "") or "")
    media_root = str(getattr(platform, "media_root", "") or "")
    puid = int(getattr(platform, "puid", 1000) or 1000)
    pgid = int(getattr(platform, "pgid", 1000) or 1000)

    for svc in services.values():
        if not isinstance(svc, dict):
            continue
        for vol in svc.get("volumes") or []:
            host = _community_volume_host_path(vol, config_root, media_root)
            if host is None:
                continue
            _ensure_community_volume_dir(host, puid, pgid, result)


def _install_inner(
    manifest: AppManifest,
    result: ExecutionResult,
    extra_env: dict[str, str] | None,
    host_port_override: int | None,
    user_volume_paths: dict[str, str] | None = None,
) -> None:
    """Inner install logic — mutates result, raises on unexpected errors.

    user_volume_paths: maps install_prompt keys to user-supplied paths (id=816).

    Orchestrates the seven install phases (validate → deps → config_dir →
    fragment → deploy → post-deploy → register) via the helpers above.
    """
    key = manifest.key

    with StateDB() as db:
        platform = db.get_platform()
        existing = db.get_app(key)

    if not _validate_install(platform, manifest, key, existing, result):
        return

    if not _install_dependencies(manifest, platform, result):
        return

    cfg = _ensure_config_dir(platform, key, result)
    if cfg is None:
        return
    config_path, dir_created_now = cfg

    if not _seed_config_files(manifest, config_path, result):  # F6b starter config
        return

    # For community (custom) apps: create any extra bind-mount directories
    # declared in the raw compose YAML before docker compose up runs.
    # Catalog apps only have config_root/<key>/ as a host bind mount (already
    # handled by _ensure_config_dir), so this is a community-only step.
    _ensure_community_compose_dirs(key, platform, result)

    extra_env = _generate_auto_secrets(manifest, extra_env)

    host_port = _compute_host_port(manifest, host_port_override)
    if not _check_port_conflict(key, host_port, result):
        return

    # Atomically claim the port to close the install-vs-install TOCTOU (#1100):
    # _check_port_conflict + deploy + _register_install are NOT one atomic step,
    # so a concurrent install of a DIFFERENT app could pass its own check for the
    # same computed port and both deploy onto it. The conditional reservation
    # (PK on port) lets exactly one racer win; the loser fails clean. Released in
    # `finally` once the app is registered (the apps-table row then owns the port).
    if host_port is not None:
        with StateDB() as _pdb:
            if not _pdb.try_reserve_port(host_port, key):
                result.fail(
                    "port_check",
                    f"Port {host_port} was just claimed by a concurrent install. Retry shortly.",
                )
                return
    try:
        service_fragment = _build_compose_service(
            manifest,
            key,
            platform,
            host_port,
            config_path,
            extra_env,
            user_volume_paths=user_volume_paths,
        )
        frag_path = _write_compose_files(key, service_fragment, result)
        if frag_path is None:
            return

        if not _run_deploy(key, frag_path, config_path, dir_created_now, result, manifest=manifest):
            return

        _run_post_deploy_steps(manifest, platform, result)
        _register_install(manifest, key, host_port, config_path, extra_env, result)
    finally:
        if host_port is not None:
            with StateDB() as _pdb:
                _pdb.release_port_reservation(host_port)


# ---------------------------------------------------------------------------
# Remove
# ---------------------------------------------------------------------------


def remove_app(key: str, delete_config: bool | None = None) -> ExecutionResult:
    """Remove an installed app.

    Steps:
      1. validate    — app exists in state DB
      2. stop        — docker compose down for this service
      3. unregister  — remove CF hostname if registered
      4. unwire      — mark wiring as stale in connected apps
      5. fragment    — remove compose fragment from disk
      6. config      — optionally delete config folder (always asks)
      7. state       — remove from state DB
    """
    result = ExecutionResult(ok=True, app_key=key, operation="remove")
    op_id: int | None = None

    # ── Validate ───────────────────────────────────────────────────────────
    with StateDB() as db:
        app = db.get_app(key)

    if app is None:
        return result.fail("validate", f"'{key}' is not installed.")

    if app.tier == 0:
        return result.fail(
            "validate", f"'{key}' is a system component and cannot be removed via the app API."
        )

    with StateDB() as db:
        op_id = db.log_operation("remove", "app", key)

    try:
        _remove_inner(key, app, result, delete_config)
    except Exception as e:
        result.fail("unexpected", "An unexpected error occurred during removal.", str(e))
        log.exception("Unexpected error removing %s", key)

    if op_id is not None:
        with StateDB() as db:
            db.complete_operation(
                op_id,
                status="completed" if result.ok else "failed",
                error=result.error or None,
            )

    return result


def _remove_unregister_hostname(key: str, result: ExecutionResult) -> Any:
    """Unregister external hostname; returns the loaded manifest (or None on failure)."""
    try:
        manifest = load_manifest(key)
        _unregister_app_hostname(key, manifest)
        result.add(
            "hostname_unregister", "ok", f"External hostname for '{key}' removed from tunnel."
        )
        return manifest
    except Exception as e:
        result.add(
            "hostname_unregister",
            "warning",
            "Could not unregister hostname — may need manual cleanup.",
            detail=str(e),
        )
        return None


def _remove_companions(manifest: Any, result: ExecutionResult) -> None:
    """Stop + remove companion containers (FerretDB, Periphery, etc.)."""
    if manifest is None:
        return
    try:
        companions = getattr(manifest, "companions", [])
        for companion in companions:
            companion_key = companion.get("key", "")
            if not companion_key:
                continue
            frag_path_c = config.compose_dir / f"{companion_key}.yaml"
            if frag_path_c.exists():
                subprocess.run(
                    # No --remove-orphans: all fragments share compose's default
                    # project (data/compose/ dir), so it would delete every other
                    # managed container (incl. Traefik) as an orphan. See
                    # backend/core/compose.py compose_down().
                    ["docker", "compose", "-f", str(frag_path_c), "down"],
                    capture_output=True,
                    timeout=30,
                )
                frag_path_c.unlink(missing_ok=True)
                log.info("Removed companion container: %s", companion_key)
    except Exception as e:
        result.add(
            "companions_remove",
            "warning",
            "Could not remove all companion containers — may need manual cleanup.",
            detail=str(e),
        )


def _remove_stop_container(key: str, result: ExecutionResult) -> None:
    """`docker compose down` for the app's fragment — if it still exists."""
    frag_path = config.compose_dir / f"{key}.yaml"
    if not frag_path.exists():
        result.add(
            "stop", "skipped", "No compose fragment found — container may already be stopped."
        )
        return
    try:
        rc, _out = compose_down(frag_path, timeout=60)
        r = type("R", (), {"returncode": rc, "stdout": _out, "stderr": ""})()
        if r.returncode == 0:
            result.add("stop", "ok", f"Container '{key}' stopped and removed.")
        else:
            # Container may already be stopped — log warning but continue
            result.add(
                "stop",
                "warning",
                "Container stop returned non-zero (may already be stopped).",
                r.stderr.strip()[:200],
            )
    except subprocess.TimeoutExpired:
        result.add(
            "stop",
            "warning",
            "Stop command timed out. Container may still be running.",
            "Run 'docker stop {key}' manually if needed.",
        )


def _remove_cf_hostname_warning(app: Any, result: ExecutionResult) -> None:
    """Surface CF tunnel hostnames that need manual removal."""
    with StateDB() as db:
        resources = db.get_active_resources(app_id=app.id)
    cf_resources = [r for r in resources if r["resource_type"] == "cf_tunnel_hostname"]
    if cf_resources:
        result.add(
            "unregister",
            "warning",
            f"{len(cf_resources)} CF hostname(s) need manual removal.",
            "Go to Cloudflare Zero Trust → Networks → Tunnels and remove the hostname(s).",
        )
    else:
        result.add("unregister", "skipped", "No external resources to clean up.")


def _remove_unwire(app: Any, result: ExecutionResult) -> None:
    """Mark wiring rows referencing this app as stale."""
    with StateDB() as db:
        if app.id:
            db.execute(
                "UPDATE wiring SET status='stale' WHERE source_app_id=? OR target_app_id=?",
                (app.id, app.id),
            )
    result.add("unwire", "ok", "Wiring entries marked stale in DB.")


def _remove_config_folder(app: Any, delete_config: bool | None, result: ExecutionResult) -> None:
    """Honor the 'ask each time' policy for config folder retention."""
    if not app.config_path:
        return
    config_dir = Path(app.config_path)
    if not config_dir.exists():
        result.add("config", "skipped", "Config folder does not exist.")
        return
    if delete_config is None:
        result.add(
            "config",
            "warning",
            f"Config folder retained at {config_dir}.",
            "Pass delete_config=True to remove it, or delete manually. "
            "Leaving it means re-installing will pick up the previous config.",
        )
        return
    if delete_config:
        try:
            shutil.rmtree(config_dir)
            result.add("config", "ok", f"Config folder deleted: {config_dir}")
        except OSError as e:
            result.add(
                "config",
                "warning",
                f"Could not delete config folder: {e}",
                "Delete manually: rm -rf " + str(config_dir),
            )
        return
    result.add("config", "skipped", f"Config folder retained at {config_dir}.")


def _remove_inner(key: str, app: Any, result: ExecutionResult, delete_config: bool | None) -> None:
    """Remove pipeline orchestrator — mirrors `_install_inner`'s per-phase split.

    Step 2.7.b: extracts the 7 phases (hostname / companions / stop /
    CF warning / unwire / fragment / config / state) into helpers.
    """
    result.add("validate", "ok", f"{app.display_name} found in state DB.")

    manifest = _remove_unregister_hostname(key, result)
    _remove_companions(manifest, result)
    _remove_stop_container(key, result)
    _remove_cf_hostname_warning(app, result)
    _remove_unwire(app, result)

    remove_fragment(key)
    result.add("fragment", "ok", "Compose fragment removed.")

    _remove_config_folder(app, delete_config, result)

    with StateDB() as db:
        db.remove_app(key)
    result.add("state", "ok", f"{app.display_name} removed from SLOP.")


# ---------------------------------------------------------------------------
# Replace
# ---------------------------------------------------------------------------


def replace_app(
    old_key: str, new_key: str, extra_env: dict[str, str] | None = None
) -> ExecutionResult:
    """Replace one app with another, preserving wiring where possible.

    Used for: Plex → Jellyfin, Portainer → Dockhand, etc.

    Steps:
      1. Validate both apps exist (old installed, new in catalog)
      2. Install new app alongside old
      3. Rewire connections from old → new where wire_type matches
      4. Remove old app (keep config folder — user decides later)
    """
    result = ExecutionResult(ok=True, app_key=new_key, operation="replace")

    with StateDB() as db:
        old_app = db.get_app(old_key)
    if old_app is None:
        return result.fail("validate", f"'{old_key}' is not installed.")

    try:
        load_manifest(new_key)
    except KeyError:
        return result.fail("validate", f"'{new_key}' not found in catalog.")

    result.add("validate", "ok", f"Replacing {old_app.display_name} with {new_key}.")

    # Reserve the shared port (if any) *before* any container operation, so a
    # concurrent replace_app / install_app sees the reservation and fails clean.
    _reserved_port = _replace_compute_shared_port(old_app, new_key, result)
    if _reserved_port is not None:
        _reserved_port = _replace_reserve_port(_reserved_port, new_key, result)

    try:
        if _reserved_port is not None:
            _replace_stop_old(old_key, new_key, _reserved_port, result)

        # Install new app
        install_result = install_app(new_key, extra_env=extra_env)
        for step in install_result.steps:
            result.steps.append(step)
        if not install_result.ok:
            result.ok = False
            result.error = f"Install of {new_key} failed: {install_result.error}"
            return result

        result.add("install_new", "ok", f"{new_key} installed successfully.")

        _replace_rewire(old_app, new_key, old_key, result)
        _replace_remove_old(old_key, result)

        result.add(
            "complete",
            "ok",
            f"Replaced {old_key} with {new_key}. "
            f"Old config retained at {old_app.config_path or 'unknown'}.",
        )
        return result

    finally:
        # Always release the port reservation — whether we succeeded or not.
        if _reserved_port is not None:
            _replace_release_port(_reserved_port)


def _replace_compute_shared_port(old_app: Any, new_key: str, result: ExecutionResult) -> int | None:
    """Determine the shared host port (if any) needing TOCTOU protection.

    Returns the port to reserve, or None when old/new don't share a non-system
    port. A reservation guards a concurrent replace_app / install_app from
    grabbing the port while the old container is being stopped.
    """
    try:
        new_manifest = load_manifest(new_key)
        old_port = old_app.host_port
        new_port = new_manifest.web_port
        if old_port and new_port and old_port == new_port and new_port not in _SYSTEM_PORTS:
            return int(old_port)
    except Exception as _pe:
        result.add("port_precheck", "warning", f"Could not determine port for reservation: {_pe}")
    return None


def _replace_reserve_port(reserved_port: int, new_key: str, result: ExecutionResult) -> int | None:
    """Write the DB port reservation *before* any container operation.

    Returns the reserved port on success, or None when the reservation failed
    (so the caller knows not to try to release it later).
    """
    try:
        with StateDB() as _rdb:
            _rdb.reserve_port(reserved_port, new_key)
        result.add("port_reserve", "ok", f"Reserved port {reserved_port} for {new_key}.")
        return reserved_port
    except Exception as _re:
        result.add("port_reserve", "warning", f"Could not reserve port {reserved_port}: {_re}")
        return None


def _replace_stop_old(
    old_key: str, new_key: str, reserved_port: int, result: ExecutionResult
) -> None:
    """Stop the old container to free the shared port. Non-fatal."""
    try:
        _old_frag = config.compose_dir / f"{old_key}.yaml"
        if _old_frag.exists():
            compose_down(_old_frag, timeout=30)
            result.add(
                "stop_old", "ok", f"Stopped {old_key} to free port {reserved_port} for {new_key}."
            )
    except Exception as _pe:
        result.add("port_precheck", "warning", f"Could not stop old container to free port: {_pe}")


def _replace_rewire(old_app: Any, new_key: str, old_key: str, result: ExecutionResult) -> None:
    """Repoint wiring rows from the old app to the new app. Non-fatal."""
    try:
        with StateDB() as db:
            new_app_rec = db.get_app(new_key)
            if new_app_rec and old_app.id and new_app_rec.id:
                db.execute(
                    "UPDATE wiring SET source_app_id=? WHERE source_app_id=?",
                    (new_app_rec.id, old_app.id),
                )
                db.execute(
                    "UPDATE wiring SET target_app_id=? WHERE target_app_id=?",
                    (new_app_rec.id, old_app.id),
                )
        result.add("rewire", "ok", f"Wiring connections updated from {old_key} → {new_key}.")
    except Exception as _we:
        result.add("rewire", "warning", f"Could not rewire connections: {_we}", detail=str(_we))


def _replace_remove_old(old_key: str, result: ExecutionResult) -> None:
    """Remove the old app (keeping its config folder). Issues are warnings."""
    remove_result = remove_app(old_key, delete_config=False)
    for step in remove_result.steps:
        step.name = f"remove_old_{step.name}"
        result.steps.append(step)
    if not remove_result.ok:
        result.add(
            "remove_old",
            "warning",
            f"New app is running but old app ({old_key}) removal had issues.",
            remove_result.error,
        )


def _replace_release_port(reserved_port: int) -> None:
    """Release a DB port reservation — best-effort, logs on failure."""
    try:
        with StateDB() as _rdb:
            _rdb.release_port_reservation(reserved_port)
    except Exception as _rle:
        log.warning("replace_app: could not release port reservation %d: %s", reserved_port, _rle)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_managed_service(service_type: str, network_name: str | None = None) -> dict[str, str]:
    """Ensure a managed service (postgres/redis) is running, deploying if needed.

    ``network_name`` — Docker network the container must join (id=464: previously
    hardcoded to STACK_NETWORK/"slop"; now caller-supplied from platform.network_name
    so the correct config-driven value is always used).  Falls back to STACK_NETWORK
    if not provided (e.g. direct calls from tests).
    """
    with StateDB() as db:
        svc = db.get_all_apps(status="running")
        running_keys = {a.key for a in svc}

    if service_type in running_keys:
        return {"status": "skipped", "message": f"Shared {service_type} already running."}

    # Use caller-supplied network; fall back to the module constant only as a
    # last resort so existing code that does not yet pass network_name still works.
    effective_network = network_name or STACK_NETWORK

    # Deploy the managed service. Tuple = (image, tag, internal_port, data_path).
    # data_path differs per engine: postgres/redis store under /data; mariadb's
    # official image stores under /var/lib/mysql (#1203).
    images = {
        "postgres": ("postgres", "16-alpine", 5432, "/data"),
        "redis": ("valkey/valkey", "8-alpine", 6379, "/data"),
        "mariadb": ("mariadb", "11.4", 3306, "/var/lib/mysql"),
    }
    image, tag, _port, data_path = images[service_type]

    # postgres/mariadb need a real DB password before the container first starts. deps are
    # provisioned BEFORE _generate_auto_secrets runs, so the managed service owns its own
    # secret-gen here (generate-if-absent → .env) rather than relying on auto_secrets. The
    # fragments reference ${POSTGRES_PASSWORD}/${MARIADB_*} with no :- default and apps wire
    # them into their DB connection (postgres: affine/umami/zilean/midarr), so without this the
    # password is EMPTY and every dependent app authenticates with no credential (#1210 postgres
    # mirrors the mariadb #1203 fix). redis (valkey) runs without auth → no managed secret.
    managed_secrets = {
        "postgres": {"POSTGRES_PASSWORD": 24},
        "mariadb": {"MARIADB_ROOT_PASSWORD": 24, "MARIADB_PASSWORD": 24},
    }
    if service_type in managed_secrets:
        _ensure_env_secrets(managed_secrets[service_type])

    fragment: dict[str, Any] = {
        "image": f"{image}:{tag}",
        "container_name": service_type,
        "restart": "unless-stopped",
        "networks": [effective_network],
        "volumes": [f"{service_type}_data:{data_path}"],
    }
    if service_type == "postgres":
        fragment["environment"] = {
            "POSTGRES_USER": "${POSTGRES_USER:-slop}",
            "POSTGRES_PASSWORD": "${POSTGRES_PASSWORD}",
        }
    elif service_type == "redis":
        fragment["command"] = "valkey-server --save 60 1 --loglevel warning"
    elif service_type == "mariadb":
        fragment["environment"] = {
            "MARIADB_ROOT_PASSWORD": "${MARIADB_ROOT_PASSWORD}",
            "MARIADB_DATABASE": "${MARIADB_DATABASE:-booklore}",
            "MARIADB_USER": "${MARIADB_USER:-booklore}",
            "MARIADB_PASSWORD": "${MARIADB_PASSWORD}",
        }

    try:
        # Named volume declared top-level so compose resolves it standalone (the
        # fragment refers to <svc>_data; without the declaration compose errors on
        # an undefined volume — latent for postgres/redis too, fixed here for all 3).
        frag_path = write_fragment(service_type, fragment, named_volumes=[f"{service_type}_data"])
        # Pull image first (long timeout) — separate from container start
        pull_rc, pull_out = compose_pull(frag_path, timeout=600)
        if pull_rc != 0:
            return {
                "status": "error",
                "message": f"Could not pull image for managed {service_type}.",
                "detail": pull_out[:300],
            }
        # Start container (short timeout, retry once)
        rc, output = compose_up(frag_path, pull=False, timeout=90)
        if rc != 0:
            # Retry once — transient daemon startup races
            rc2, _output2 = compose_up(frag_path, pull=False, timeout=60)
            if rc2 != 0:
                return {
                    "status": "error",
                    "message": f"Could not start managed {service_type}.",
                    "detail": output[:300],
                }

        # Wait up to 60s for healthy — it's a cold pull on first use
        for _ in range(30):
            c = docker_client.get_container(service_type)
            if c and c.status == "running":
                break
            time.sleep(2)
        else:
            return {
                "status": "error",
                "message": f"Managed {service_type} started but not healthy after 60s.",
                "detail": "Check: docker logs " + service_type,
            }

        with StateDB() as db:
            db.upsert_app(
                service_type,
                display_name=service_type.title(),
                tier=0,
                category="managed",
                status="running",
                image=image,
                image_tag=tag,
                container_name=service_type,
                web_port=None,
                host_port=None,
                config_path=None,
                manifest_source="internal",
            )

        return {"status": "ok", "message": f"Managed {service_type} deployed."}
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to deploy managed {service_type}.",
            "detail": str(e),
        }


def _apply_companion_dir_perms(host: str, run_uid: Any, run_gid: Any) -> None:
    """chown/chmod a companion bind-mount dir.

    With both run_as_uid/run_as_gid set: chown + chmod 0o755 (least-privilege).
    Otherwise: chmod 0o777 (world-writable) — works but the container UID is
    unknown, so annotate companions with run_as_uid/run_as_gid to tighten this.
    """
    if run_uid is not None and run_gid is not None:
        os.chown(host, int(run_uid), int(run_gid))
        os.chmod(host, 0o755)  # noqa: S103  # nosec B103  # container UID mapped; 0o755 required for service access
    else:
        os.chmod(host, 0o777)  # noqa: S103  # nosec B103  # world-writable required: container UID unknown


def _build_companion_volumes(companion: dict[str, Any], platform: Any) -> list[str]:
    """Expand a companion's volume list, creating + chowning host dirs as needed.

    Returns the docker-compose ``volumes`` list (``host:container[:ro]`` strings).
    """
    vols = companion.get("volumes", [])
    # Per-companion UID/GID — set in YAML when the container user is known.
    run_uid = companion.get("run_as_uid")
    run_gid = companion.get("run_as_gid")
    expanded: list[str] = []
    for v in vols:
        host = _expand_path(v["host"], platform)
        container = v["container"]
        ro = ":ro" if v.get("readonly") else ""
        if not os.path.exists(host):
            os.makedirs(host, exist_ok=True)
            _apply_companion_dir_perms(host, run_uid, run_gid)
        elif os.path.isdir(host):
            _apply_companion_dir_perms(host, run_uid, run_gid)
        # socket/device paths (non-directory): leave permissions as-is
        expanded.append(f"{host}:{container}{ro}")
    return expanded


def _build_companion_fragment(companion: dict[str, Any], platform: Any) -> dict[str, Any]:
    """Assemble the docker-compose service fragment for one companion.

    Raises ValueError when the companion's ``command`` value fails sanitization.
    """
    ckey = companion.get("key", "")
    image = companion.get("image", "")
    tag = companion.get("image_tag", "latest")

    cmd_raw = companion.get("command")
    cmd = _sanitize_companion_command(str(cmd_raw)) if cmd_raw is not None else None

    frag: dict[str, Any] = {
        "image": f"{image}:{tag}",
        "container_name": ckey,
        "restart": "unless-stopped",
        "networks": [platform.network_name],
    }

    vols = _build_companion_volumes(companion, platform)
    if vols:
        frag["volumes"] = vols

    env = companion.get("env", {})
    if env:
        frag["environment"] = env

    if cmd:
        frag["command"] = cmd

    return frag


def _deploy_companion(
    companion: dict[str, Any], manifest: AppManifest, platform: Any
) -> dict[str, str] | None:
    """Build + start a single companion. Returns an error dict, or None on success."""
    ckey = companion.get("key", "")
    try:
        frag = _build_companion_fragment(companion, platform)
    except ValueError as _e:
        log.error(
            "manifest %s: companion '%s' command rejected by sanitizer: %s",
            manifest.key,
            ckey,
            _e,
        )
        return {
            "status": "error",
            "message": f"Companion '{ckey}' has an unsafe command value.",
            "detail": str(_e),
        }

    try:
        frag_path = write_fragment(ckey, frag)
        r = subprocess.run(
            ["docker", "compose", "-f", str(frag_path), "up", "-d", "--pull", "missing"],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if r.returncode != 0:
            return {
                "status": "error",
                "message": f"Could not start companion '{ckey}'.",
                "detail": r.stderr.strip()[:300],
            }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to deploy companion '{ckey}'.",
            "detail": str(e),
        }
    return None


def _deploy_companions(manifest: AppManifest, platform: Any) -> dict[str, str]:
    """Deploy app-specific companion containers concurrently."""
    if not manifest.companions:
        return {"status": "ok", "message": "No companions."}
    errors: list[str] = []
    with ThreadPoolExecutor(max_workers=len(manifest.companions)) as pool:
        futures = {
            pool.submit(_deploy_companion, c, manifest, platform): c for c in manifest.companions
        }
        for fut in as_completed(futures):
            err = fut.result()
            if err is not None:
                errors.append(err["message"])
    if errors:
        return {"status": "error", "message": "; ".join(errors)}
    return {"status": "ok", "message": f"Deployed {len(manifest.companions)} companion(s)."}


def _docker_compose_up(key: str, frag_path: Path, pull_timeout: int = 600) -> dict[str, str]:
    """Pull image then start container for a single service fragment.

    Pull and start are separate subprocesses so that large images (e.g. Ollama)
    get a long pull timeout without inflating the container-start deadline.
    """
    try:
        pull_rc, pull_output = compose_pull(frag_path, timeout=pull_timeout)
        if pull_rc != 0:
            return {
                "status": "error",
                "message": f"Failed to pull image for {key}.",
                "detail": _clean_compose_output(pull_output),
            }
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to pull image for {key}.",
            "detail": str(e),
        }
    try:
        rc, output = compose_up(frag_path, pull=False, timeout=120)
        if rc != 0:
            return {
                "status": "error",
                "message": f"Failed to start {key}.",
                "detail": _clean_compose_output(output),
            }
        return {"status": "ok", "message": f"Container '{key}' started."}
    except Exception as e:
        return {
            "status": "error",
            "message": f"Failed to start {key}.",
            "detail": str(e),
        }


def _run_post_deploy_step(step: Any, manifest: AppManifest, platform: Any) -> dict[str, str]:
    """Execute a single post-deploy step."""
    if step.step_type == "wait_healthy":
        return _wait_healthy(manifest.key, step.timeout)
    elif step.step_type == "api_ready":
        return _wait_api_ready(manifest.key, step.path, step.timeout, platform)
    elif step.step_type == "wire":
        return _wire(manifest.key, step.target, step.wire_type)
    elif step.step_type == "custom":
        log.warning(
            "manifest %s: custom post-deploy step encountered but not yet implemented", manifest.key
        )
        return {
            "status": "warning",
            "message": "Custom steps not yet implemented.",
            "warning": "custom post-deploy steps not yet implemented",
        }
    return {"status": "skipped", "message": f"Unknown step type: {step.step_type}"}


def _wait_healthy(key: str, timeout: int) -> dict[str, str]:
    deadline = time.monotonic() + timeout
    iteration = 0
    while time.monotonic() < deadline:
        try:
            c = docker_client.get_container(key)
        except Exception as _de:
            return {
                "status": "error",
                "message": f"Cannot check container '{key}': Docker unavailable.",
                "detail": str(_de),
            }
        if c:
            if c.health in ("healthy", "none") and c.status == "running":
                return {"status": "ok", "message": f"Container '{key}' is healthy."}
            if c.status in ("exited", "dead"):
                try:
                    logs = docker_client.container_logs(key, tail=10)
                except Exception:
                    logs = "(logs unavailable)"
                return {
                    "status": "error",
                    "message": f"Container '{key}' exited unexpectedly.",
                    "detail": logs,
                }
        time.sleep(1 if iteration < 2 else 3)
        iteration += 1
    return {
        "status": "error",
        "message": f"Container '{key}' did not become healthy within {timeout}s.",
        "detail": "Check logs with: docker logs " + key,
    }


def _wait_api_ready(key: str, path: str, timeout: int, platform: Any) -> dict[str, str]:
    """Poll the app's API until it responds."""
    with StateDB() as db:
        app = db.get_app(key)
    if app is None or not app.host_port:
        return {"status": "skipped", "message": "No port available for API check."}

    url = f"http://localhost:{app.host_port}{path}"
    deadline = time.monotonic() + timeout

    while time.monotonic() < deadline:
        try:
            r = httpx.get(url, timeout=3, follow_redirects=True)
            if r.status_code < 500:
                return {"status": "ok", "message": f"API ready at {url}"}
        except Exception as _e:
            log.debug("Suppressed exception: %s", _e)
        time.sleep(2)

    return {
        "status": "error",
        "message": f"{key} API did not respond within {timeout}s.",
        "detail": f"Tried: {url}. Check logs with: docker logs {key}",
    }


# App-to-app wiring pass extracted to executor_wiring.py (#1302 drain) —
# re-exported below.


def _expand_path(path: str, platform: Any) -> str:
    """Expand manifest path placeholders with platform values."""
    return path.replace("{config_root}", platform.config_root or "").replace(
        "{media_root}", platform.media_root or ""
    )


def _clean_compose_output(raw: str) -> str:
    """Strip progress noise from Docker Compose stderr."""
    keep = []
    for line in raw.splitlines():
        s = line.strip()
        if not s:
            continue
        if any(
            s.startswith(p)
            for p in (
                "Pulling",
                "Waiting",
                "Downloading",
                "Extracting",
                "Pull complete",
                "Already exists",
                "Digest:",
                "Status:",
                "⠋",
                "⠙",
                "⠹",
                "⠸",
                "⠼",
                "⠴",
                "⠦",
                "⠧",
                "⠇",
                "⠏",
            )
        ):
            continue
        keep.append(s)
    return "\n".join(keep) if keep else raw[:400]


# ---------------------------------------------------------------------------
# Criticality classification
# ---------------------------------------------------------------------------


from enum import Enum  # noqa: E402  # deferred: Criticality class defined after install machinery


class Criticality(str, Enum):  # noqa: UP042  # str+Enum pattern kept for JSON serialisation compatibility
    INVIOLABLE = "inviolable"  # cannot disable — stack dies
    IMPORTANT = "important"  # warn strongly before disable
    INDEPENDENT = "independent"  # disable freely, no stack impact
    ENHANCEMENT = "enhancement"  # disable has zero availability impact


_CRITICALITY_MAP: dict[str, Criticality] = {
    "traefik": Criticality.INVIOLABLE,
    "tinyauth": Criticality.IMPORTANT,
    "authelia": Criticality.IMPORTANT,
    "authentik": Criticality.IMPORTANT,
    "oauth2-proxy": Criticality.IMPORTANT,
    "cloudflared": Criticality.IMPORTANT,
    "tailscale": Criticality.IMPORTANT,
    "headscale": Criticality.IMPORTANT,
    "netbird": Criticality.IMPORTANT,
    "zerotier": Criticality.IMPORTANT,
    "pangolin": Criticality.IMPORTANT,
    "nebula": Criticality.IMPORTANT,
    "gluetun": Criticality.IMPORTANT,
    "portainer": Criticality.IMPORTANT,
    "dockge": Criticality.ENHANCEMENT,
    "dockhand": Criticality.ENHANCEMENT,
    "komodo": Criticality.ENHANCEMENT,
    "homepage": Criticality.ENHANCEMENT,
    "glance": Criticality.ENHANCEMENT,
    "ollama": Criticality.ENHANCEMENT,
    "llamacpp_server": Criticality.ENHANCEMENT,
    "configarr": Criticality.ENHANCEMENT,
    "recyclarr": Criticality.ENHANCEMENT,
    "watchtower": Criticality.ENHANCEMENT,
    "tdarr": Criticality.ENHANCEMENT,
    "fileflows": Criticality.ENHANCEMENT,
    "netdata": Criticality.ENHANCEMENT,
    "dozzle": Criticality.ENHANCEMENT,
    "beszel": Criticality.ENHANCEMENT,
    "speedtest_tracker": Criticality.ENHANCEMENT,
    "changedetection": Criticality.ENHANCEMENT,
    "glance": Criticality.ENHANCEMENT,
    "umami": Criticality.ENHANCEMENT,
    "litlyx": Criticality.ENHANCEMENT,
}


def get_criticality(key: str) -> Criticality:
    """Return the criticality level for an app key."""
    return _CRITICALITY_MAP.get(key, Criticality.INDEPENDENT)


# ---------------------------------------------------------------------------
# Graceful disable / enable
# ---------------------------------------------------------------------------


import dataclasses as _dc  # noqa: E402  # deferred: DisableResult defined after install/replace machinery


@_dc.dataclass
class DisableResult:
    key: str
    ok: bool
    criticality: str = ""
    error: str | None = None


# Performance thresholds that trigger auto-disable recommendation
PERF_THRESHOLDS = {
    "cpu_percent_sustained": 85.0,  # % CPU for 10+ min
    "oom_kills_per_hour": 3,  # OOM kills within 1hr
    "api_response_seconds": 10.0,  # health check response time
    "llm_inference_seconds": 45.0,  # LLM call timeout
    "llm_parse_fail_streak": 3,  # consecutive bad JSON responses
}


def disable_app(key: str, reason: str = "user_request") -> DisableResult:
    """Gracefully disable an app without removing it.

    Stops the container and renames the compose fragment to .yaml.disabled.
    Config folder and state DB record are preserved.
    Wiring entries are marked stale — re-established on enable.
    One-click re-enable via enable_app().

    Performance-triggered disables use reason='performance' or 'health'.
    The health system calls this when PERF_THRESHOLDS are exceeded.
    """
    criticality = get_criticality(key)

    if criticality == Criticality.INVIOLABLE:
        return DisableResult(
            key=key,
            ok=False,
            criticality=criticality.value,
            error=(
                f"'{key}' cannot be disabled — it is required for the stack to function. "
                f"Disabling Traefik would take down all services."
            ),
        )

    with StateDB() as db:
        app = db.get_app(key)

    if not app:
        return DisableResult(
            key=key, ok=False, criticality=criticality.value, error=f"App '{key}' is not installed."
        )

    if app.status == "disabled":
        return DisableResult(key=key, ok=True, criticality=criticality.value)

    # Stop the container (non-fatal if already stopped or Docker unavailable)
    import subprocess

    try:
        subprocess.run(["docker", "stop", "-t", "10", key], capture_output=True, timeout=20)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass  # Docker not available — container may already be stopped

    # Rename fragment so compose doesn't auto-restart it
    frag_path = config.compose_dir / f"{key}.yaml"
    if frag_path.exists():
        frag_path.rename(frag_path.with_suffix(".yaml.disabled"))

    # Mark wiring stale — dependent apps continue working
    with StateDB() as db:
        if app.id:
            db.execute(
                "UPDATE wiring SET status='stale' WHERE source_app_id=? OR target_app_id=?",
                (app.id, app.id),
            )
        db.upsert_app(key, status="disabled")
        db.upsert_health_check(
            "app",
            key,
            "availability",
            status="warning",
            summary=f"Disabled ({reason}). Re-enable in SLOP UI.",
            auto_fix="enable_app",
        )
        db.log_operation(
            "disable",
            "app",
            key,
            triggered_by="health" if reason in ("performance", "health") else "user",
            detail={"reason": reason, "criticality": criticality.value},
        )

    log.info("Disabled %s (reason=%s, criticality=%s)", key, reason, criticality.value)
    return DisableResult(key=key, ok=True, criticality=criticality.value)


def enable_app(key: str) -> DisableResult:
    """Re-enable a previously disabled app."""
    criticality = get_criticality(key)

    with StateDB() as db:
        app = db.get_app(key)

    if not app:
        return DisableResult(
            key=key, ok=False, criticality=criticality.value, error=f"App '{key}' is not installed."
        )

    if app.status != "disabled":
        return DisableResult(key=key, ok=True, criticality=criticality.value)

    # Restore the fragment
    disabled_path = config.compose_dir / f"{key}.yaml.disabled"
    frag_path = config.compose_dir / f"{key}.yaml"
    if disabled_path.exists():
        disabled_path.rename(frag_path)
    elif not frag_path.exists():
        return DisableResult(
            key=key,
            ok=False,
            criticality=criticality.value,
            error=(f"Compose fragment not found for '{key}'. The app may need to be reinstalled."),
        )

    # Start the container
    try:
        import subprocess

        result = subprocess.run(
            ["docker", "compose", "-f", str(frag_path), "up", "-d"],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            raise RuntimeError(_clean_compose_output(result.stderr or result.stdout))
    except Exception as e:
        with StateDB() as db:
            db.upsert_app(key, status="error")
        return DisableResult(
            key=key,
            ok=False,
            criticality=criticality.value,
            error=f"Could not start {key}: {e}",
        )

    # Restore wiring to pending (wiring engine reconnects on next health run)
    with StateDB() as db:
        if app.id:
            db.execute(
                "UPDATE wiring SET status='pending' WHERE source_app_id=? OR target_app_id=?",
                (app.id, app.id),
            )
        db.upsert_app(key, status="running")
        db.upsert_health_check(
            "app",
            key,
            "availability",
            status="ok",
            summary="Re-enabled successfully.",
        )
        db.log_operation("enable", "app", key, triggered_by="user")

    log.info("Re-enabled %s", key)
    return DisableResult(key=key, ok=True, criticality=criticality.value)


# ---------------------------------------------------------------------------
# Multi-instance support
# ---------------------------------------------------------------------------


import dataclasses as _dc  # noqa: E402  # deferred: InstanceResult defined after disable/enable machinery


@_dc.dataclass
class InstanceResult:
    instance_key: str
    manifest_key: str
    ok: bool
    role: str = "default"
    error: str | None = None


def install_instance(
    manifest_key: str,
    instance_key: str,
    instance_label: str,
    role: str = "default",
    extra_env: dict[str, str] | None = None,
    host_port_override: int | None = None,
) -> InstanceResult:
    """Install a named instance of an app manifest.

    Allows multiple deployments of the same manifest with different keys,
    labels, and roles (debrid / download / secondary).

    Example:
        install_instance("sonarr", "sonarr_debrid", "Sonarr (Debrid)", role="debrid")
        install_instance("sonarr", "sonarr_download", "Sonarr (Download)", role="download")

    The instance_key becomes the container name, config path, and state DB key.
    The manifest_key is the base manifest used for configuration.
    The app_instances table records the relationship for routing queries.
    """
    if role not in ("default", "debrid", "download", "secondary"):
        return InstanceResult(
            instance_key=instance_key,
            manifest_key=manifest_key,
            ok=False,
            role=role,
            error=f"Invalid role '{role}'. Must be: default, debrid, download, secondary",
        )

    # Check not already installed under instance_key
    with StateDB() as db:
        existing = db.get_app(instance_key)
    if existing and existing.status not in ("error",):
        return InstanceResult(
            instance_key=instance_key,
            manifest_key=manifest_key,
            ok=False,
            role=role,
            error=f"Instance '{instance_key}' is already installed.",
        )

    # Load base manifest and patch key for this instance
    try:
        manifest = load_manifest(manifest_key)
    except KeyError:
        return InstanceResult(
            instance_key=instance_key,
            manifest_key=manifest_key,
            ok=False,
            role=role,
            error=f"No manifest '{manifest_key}' found in catalog.",
        )

    # Temporarily reassign the key so the executor uses instance_key everywhere
    import copy
    import dataclasses as _dc2

    inst_manifest = copy.copy(manifest)
    inst_manifest = _dc2.replace(inst_manifest, key=instance_key)

    # Check port override doesn't conflict before attempting install
    if host_port_override:
        try:
            from backend.core import docker_client as _dc_mod

            in_use = _dc_mod.ports_in_use()
        except Exception:
            in_use = {}
        if host_port_override in in_use:
            return InstanceResult(
                instance_key=instance_key,
                manifest_key=manifest_key,
                ok=False,
                role=role,
                error=(
                    f"Port {host_port_override} is already in use by "
                    f"'{in_use[host_port_override]}'. Choose a different port."
                ),
            )
        # Also check DB for reserved ports
        with StateDB() as _pdb:
            _db_owner = _pdb._c.execute(
                "SELECT key FROM apps WHERE host_port=? AND key!=? AND status NOT IN ('disabled','failed','removing')",
                (host_port_override, instance_key),
            ).fetchone()
        if _db_owner:
            return InstanceResult(
                instance_key=instance_key,
                manifest_key=manifest_key,
                ok=False,
                role=role,
                error=(
                    f"Port {host_port_override} is reserved by app '{_db_owner[0]}'. "
                    f"Choose a different port."
                ),
            )

    result = ExecutionResult(ok=True, app_key=instance_key, operation="install")
    try:
        _install_inner(inst_manifest, result, extra_env, host_port_override)
    except Exception as e:
        result.fail("unexpected", "Unexpected error installing instance.", str(e))

    if result.ok:
        # Record in app_instances table — use a single StateDB context so both
        # writes commit atomically (eliminates the race condition window).
        with StateDB() as db:
            db.execute(
                """
                INSERT OR REPLACE INTO app_instances
                    (instance_key, manifest_key, label, role)
                VALUES (?, ?, ?, ?)
                """,
                (instance_key, manifest_key, instance_label, role),
            )
            # Update app record — status written in same transaction as app_instances
            db.upsert_app(instance_key, status="running")

    return InstanceResult(
        instance_key=instance_key,
        manifest_key=manifest_key,
        ok=result.ok,
        role=role,
        error=result.error,
    )


def list_instances() -> list[dict[str, Any]]:
    """Return all app instances with their manifest_key, role, and status."""
    with StateDB() as db:
        rows = db.execute(
            """
            SELECT ai.instance_key, ai.manifest_key, ai.label, ai.role,
                   a.status, a.host_port, a.web_port
            FROM app_instances ai
            LEFT JOIN apps a ON a.key = ai.instance_key
            ORDER BY ai.manifest_key, ai.role
            """
        ).fetchall()
    return [
        {
            "instance_key": r[0],
            "manifest_key": r[1],
            "label": r[2],
            "role": r[3],
            "status": r[4] or "unknown",
            "host_port": r[5],
            "web_port": r[6],
        }
        for r in rows
    ]


def get_instances_for_manifest(manifest_key: str) -> list[dict[str, Any]]:
    """Return all installed instances of a given manifest key."""
    return [i for i in list_instances() if i["manifest_key"] == manifest_key]


# ---------------------------------------------------------------------------
# CF hostname auto-registration
# ---------------------------------------------------------------------------


def _get_active_tunnel_provider() -> InfraProvider | None:
    """Return the active tunnel provider, or None if not configured."""
    try:
        from backend.core.state import StateDB
        from backend.infra.registry import get_provider

        with StateDB() as db:
            slot = db.get_slot("tunnel")
        if slot.status == "active" and slot.provider:
            return get_provider("tunnel", slot.provider)
    except Exception as e:
        log.debug("Could not load tunnel provider: %s", e)
    return None


def _register_app_hostname(key: str, manifest: Any, platform: Any) -> StepLog:
    """Register app hostname in the active tunnel (CF or Tailscale).

    For service_type='management': registers in CF Tunnel with ForwardAuth.
    For service_type='media': creates DNS-only A record (direct TLS, no tunnel).
    For service_type='internal': skips — LAN only.
    Called after successful install.
    Respects the cf_auto_register_hostnames setting (default: True).
    """
    step = StepLog(
        name="hostname_register",
        status="skipped",
        message="No active tunnel provider — skipping hostname registration.",
    )

    service_type = getattr(manifest, "service_type", "management")
    if service_type == "internal":
        step.message = "Internal app — no external hostname needed."
        return step

    # Respect the auto-register setting (can be disabled in Settings UI)
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            auto = db.get_setting("cf_auto_register_hostnames")
        if auto is not None and auto.lower() in ("false", "0", "no"):
            step.status = "skipped"
            step.message = (
                "Hostname auto-registration is disabled. "
                "Enable it in Settings → Cloudflare Integration."
            )
            return step
    except Exception as _e:
        log.debug("Suppressed exception: %s", _e)  # if setting unreadable, default to enabled

    provider = _get_active_tunnel_provider()
    if provider is None:
        step.message = "No tunnel active — apps accessible on LAN only."
        return step

    domain = getattr(platform, "domain", "") or ""
    if not domain:
        step.status = "skipped"
        step.message = "Domain not configured — skipping hostname registration."
        return step

    subdomain = getattr(manifest, "traefik_subdomain", "") or key
    hostname = f"{subdomain}.{domain}"
    port = getattr(manifest, "web_port", None) or 80
    target = f"http://{key}:{port}"

    if service_type == "media":
        # Media apps need a DNS-only A record — NOT a CF Tunnel ingress.
        # Direct connection: client → server port 443 → Traefik → container.
        # The CF provider's register_hostname creates a proxied CNAME which
        # would route through CF CDN (ToS violation for video). Instead we
        # call a dedicated method that creates an unproxied A record.
        if hasattr(provider, "register_dns_only_record"):
            result = provider.register_dns_only_record(hostname)
        else:
            step.status = "skipped"
            step.message = (
                f"Media app '{key}' needs a DNS-only A record for {hostname}. "
                f"Set this manually in your DNS provider: A record → your server IP, proxy OFF."
            )
            return step
    else:
        result = provider.register_hostname(hostname, target)

    step.status = "ok" if result.ok else "warning"
    step.message = result.message
    if not result.ok:
        step.detail = result.detail
        log.warning("Hostname registration for %s: %s", key, result.message)
    else:
        log.info("Hostname registered: %s → %s", hostname, target)

    return step


def _unregister_app_hostname(key: str, manifest: Any) -> None:
    """Remove CF Tunnel hostname on app removal. Non-fatal — logs on error."""
    service_type = getattr(manifest, "service_type", "management")
    if service_type == "internal":
        return

    provider = _get_active_tunnel_provider()
    if provider is None:
        return

    try:
        with StateDB() as db:
            platform = db.get_platform()
        domain = getattr(platform, "domain", "") or ""
        if not domain:
            return

        subdomain = getattr(manifest, "traefik_subdomain", "") or key
        hostname = f"{subdomain}.{domain}"
        result = provider.unregister_hostname(hostname)
        if result.ok:
            log.info("Hostname unregistered: %s", hostname)
        else:
            log.warning("Could not unregister hostname %s: %s", hostname, result.message)
    except Exception as e:
        log.warning("Error unregistering hostname for %s: %s", key, e)


# ---------------------------------------------------------------------------
# Smoke tests
# ---------------------------------------------------------------------------


def _run_smoke_test(key: str, manifest: Any, api_ready_passed: bool = False) -> StepLog:
    """Quick post-install verification that the app is actually responding.

    Smoke tests differ from ongoing health checks:
    - Run ONCE immediately after install (not on a schedule)
    - Check the primary API endpoint with a 15-second deadline
    - Record result in health_checks table as 'smoke_test'
    - Mark app 'unhealthy' on failure so the dashboard shows it clearly

    Returns a StepLog — never raises.
    """
    import time
    import socket

    step = StepLog(name="smoke_test", status="ok", message="Smoke test passed.")

    web_port = getattr(manifest, "web_port", None)
    if not web_port:
        step.status = "skipped"
        step.message = "No web port defined — skipping smoke test."
        return step

    # GG: skip TCP smoke test for apps on system ports — Traefik owns them on the host
    if web_port in _SYSTEM_PORTS:
        step.status = "skipped"
        step.message = (
            f"System-port app (port {web_port}) — "
            f"smoke test via Traefik is not reliable from host. "
            f"Check: docker logs {key}"
        )
        return step

    # Use the host-mapped port — SLOP runs on the host, not inside Docker.
    # The container-internal port (web_port) is only reachable from within
    # the Docker network. We need the host port to reach it from here.
    with StateDB() as _db:
        _app_rec = _db.get_app(key)
    host_port = (getattr(_app_rec, "host_port", None) or web_port) if _app_rec else web_port
    check_host = "localhost"

    # Use manifest start_grace_s for the deadline — apps like Immich need 120s
    smoke_deadline_s = int(getattr(manifest, "start_grace_s", 30) or 30)
    # First: TCP connectivity check
    if api_ready_passed:
        tcp_ok = True  # api_ready already confirmed connectivity; skip loop
    else:
        deadline = time.monotonic() + smoke_deadline_s
        tcp_ok = False
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((check_host, host_port), timeout=2):
                    tcp_ok = True
                    break
            except (ConnectionRefusedError, OSError):
                time.sleep(1)

    if not tcp_ok:
        step.status = "error"
        step.message = (
            f"{manifest.display_name} is not listening on port {host_port} "
            f"after {smoke_deadline_s}s. Container may have crashed on startup."
        )
        step.detail = (
            f"TCP connection to localhost:{host_port} refused or timed out. "
            f"Check logs: docker logs {key}"
        )
        with StateDB() as db:
            db.upsert_app(key, status="unhealthy")
            db.upsert_health_check(
                "app",
                key,
                "smoke_test",
                status="error",
                summary=step.message,
                auto_fix="restart",
            )
        log.warning("Smoke test FAILED for %s: TCP connection refused on :%d", key, web_port)
        return step

    # TCP is open — check HTTP if there are health check paths defined
    health_checks = getattr(manifest, "health_checks", [])
    http_check = next((h for h in health_checks if h.check_type == "http"), None)
    if http_check:
        try:
            import urllib.request

            url = f"http://localhost:{host_port}{http_check.path}"
            if not url.startswith(("http://", "https://")):
                raise ValueError(f"Unsupported URL scheme: {url}")
            req = urllib.request.Request(url, method="GET")  # noqa: S310  # scheme validated above; localhost only
            with urllib.request.urlopen(req, timeout=5) as resp:  # noqa: S310  # nosec B310  # scheme validated above; localhost only
                if resp.status == http_check.expect_status:
                    step.message = (
                        f"{manifest.display_name} responding correctly "
                        f"(HTTP {resp.status} on {http_check.path})."
                    )
                else:
                    step.status = "warning"
                    step.message = (
                        f"{manifest.display_name} returned HTTP {resp.status} "
                        f"(expected {http_check.expect_status}). May still be initialising."
                    )
        except Exception as e:
            # HTTP check failed but TCP worked — likely still starting up
            step.status = "warning"
            step.message = (
                f"{manifest.display_name} port {web_port} is open but HTTP "
                f"check failed. This is normal if the app is still initialising."
            )
            step.detail = str(e)
    else:
        step.message = f"{manifest.display_name} port {web_port} is accepting connections."

    # Record in health_checks table
    with StateDB() as db:
        db.upsert_health_check(
            "app",
            key,
            "smoke_test",
            status=step.status if step.status != "error" else "error",
            summary=step.message,
        )

    log.info("Smoke test %s for %s (port %d)", step.status.upper(), key, web_port)
    return step
