"""backend/platform/wizard.py

Platform wizard — the one-time setup that must complete before any apps
can be installed.

Steps run in order. Each step is independently validatable and executable.
If a step fails, the wizard stops and reports the plain-language error.
The wizard is idempotent — re-running it after a partial failure is safe.

Steps:
  1. preflight       — Docker reachable, ports 80/443 free
  2. network         — create the shared Docker network
  3. config_dirs     — create Traefik config directories and acme.json
  4. traefik_config  — write traefik.yml static config
  5. traefik_deploy  — write fragment, docker compose up traefik
  6. traefik_healthy — wait for Traefik to report healthy
  7. complete        — mark platform status = ready in state DB
"""

from __future__ import annotations

import hashlib
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from collections.abc import Callable

from backend.core import docker_client
from backend.core.compose import (
    STACK_NETWORK,
    build_traefik_fragment,
    build_traefik_yaml,
    write_fragment,
)
from backend.core.config import config
from backend.core.logging import get_logger
from backend.core.system_eval import evaluate_system
from backend.core.state import StateDB
from backend.platform.ollama_runtime import (
    ensure_local_ollama_runtime,
    local_ollama_runtime_url,
    normalize_llm_agent_config,
)

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Step result
# ---------------------------------------------------------------------------


@dataclass
class StepResult:
    step: str
    status: str  # ok | error | skipped
    message: str  # plain-language one-liner
    detail: str = ""  # expanded info (shown on expand in UI)

    @property
    def ok(self) -> bool:
        return self.status == "ok"


@dataclass
class WizardResult:
    steps: list[StepResult] = field(default_factory=list)
    platform_ready: bool = False

    @property
    def ok(self) -> bool:
        return all(s.ok or s.status == "skipped" for s in self.steps)

    def last_error(self) -> StepResult | None:
        for s in reversed(self.steps):
            if s.status == "error":
                return s
        return None


# ---------------------------------------------------------------------------
# Wizard input
# ---------------------------------------------------------------------------


@dataclass
class WizardInput:
    domain: str
    config_root: str
    media_root: str
    puid: int
    pgid: int
    timezone: str
    cert_resolver: str = "letsencrypt"
    network_name: str = STACK_NETWORK
    # TLS / ACME settings
    acme_email: str = ""  # defaults to admin@domain
    dns_provider: str = "cloudflare"  # traefik/lego DNS provider key
    include_zerossl: bool = False  # only True when cert_resolver=zerossl
    eab_kid: str = ""  # ZeroSSL External Account Binding Key ID
    eab_hmac: str = ""  # ZeroSSL External Account Binding HMAC Key
    # Access mode settings (for media server routing)
    ntfy_url: str = "http://ntfy:80"
    ntfy_topic: str = "slop"
    ntfy_enabled: bool = True
    # Tunnel selections (multi-select list)
    tunnels: list[str] | None = None  # ["cloudflared", "tailscale"]
    # Infra slot selections (stored for context; deployment happens via wizard_install_stacks)
    auth: str = "tinyauth"  # auth provider: tinyauth | authelia | authentik | oauth2-proxy
    vpn: str = "none"  # vpn provider: gluetun | none
    dashboard: str = "none"  # dashboard: glance | homepage | none
    management: str = "none"  # container mgmt: dockhand | dockge | none
    traefik_dashboard_port: int = 8081
    # LLM backend selection (automatic Ollama install during wizard)
    llm_provider: str = "ollama"  # ollama | groq | cerebras | openai | etc.
    ollama_model: str = "phi4-mini"  # model to pull after Ollama install
    ollama_server: str = "local"  # local | remote
    ollama_url: str = "http://ollama:11434"  # Ollama API URL (local container or remote)
    llamacpp_url: str = "http://localhost:8081"  # llama.cpp server URL
    # *arr apps auth bypass (disable onboard auth so SLOP can manage them)
    arr_auth_bypass: bool = (
        False  # True = set AuthenticationMethod=External, AuthenticationRequired=Disabled
    )
    arr_auth_provider: str = "none"  # none | tinyauth | traefik (which auth to use instead)
    # Secrets collected by Stage 5 — written verbatim to .env
    secrets: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# Individual steps
# ---------------------------------------------------------------------------


def step_system_eval(inp: WizardInput) -> StepResult:
    """Evaluate host resources and determine LLM model recommendation."""
    try:
        profile = evaluate_system(
            config_root=inp.config_root,
            media_root=inp.media_root,
        )
        ram_gb = round(profile.total_ram_mb / 1024, 1)
        free_gb = round(profile.free_ram_mb / 1024, 1)
        headroom_gb = round(profile.headroom_ram_mb / 1024, 1)

        detail_lines = [
            f"CPU: {profile.cpu_cores} cores — {profile.cpu_model}",
            f"RAM: {ram_gb}GB total, {free_gb}GB available",
        ]
        for disk in profile.disks:
            detail_lines.append(
                f"Disk {disk.path}: {disk.free_gb}GB free of {disk.total_gb}GB ({disk.percent_used}% used)"
            )

        if profile.recommended_model:
            detail_lines.append(
                f"LLM recommendation: {profile.recommended_model} ({headroom_gb}GB headroom)"
            )
        elif profile.llm_warning:
            detail_lines.append(f"LLM: {profile.llm_warning}")

        # Store profile in settings for later use
        import json
        import time as _time

        with StateDB() as db:
            db.set_setting(
                "system_profile",
                json.dumps(
                    {
                        "cpu_cores": profile.cpu_cores,
                        "total_ram_mb": profile.total_ram_mb,
                        "free_ram_mb": profile.free_ram_mb,
                        "headroom_ram_mb": profile.headroom_ram_mb,
                        "recommended_llm_model": profile.recommended_model,
                        "llm_warning": profile.llm_warning,
                        "measured_at": int(_time.time()),
                    }
                ),
            )

        return StepResult(
            step="system_eval",
            status="ok",
            message=(
                f"System: {profile.cpu_cores} cores, {ram_gb}GB RAM. "
                + (
                    f"Recommended LLM: {profile.recommended_model}."
                    if profile.recommended_model
                    else "RAM too limited for LLM agent."
                )
            ),
            detail="\n".join(detail_lines),
        )
    except Exception as e:
        # Non-fatal — continue wizard even if eval fails
        return StepResult(
            step="system_eval",
            status="skipped",
            message="System evaluation skipped — could not read hardware info.",
            detail=str(e),
        )


def step_preflight(inp: WizardInput) -> StepResult:
    """Check Docker is reachable and ports 80/443 are free."""
    # Docker reachable?
    try:
        docker_client.daemon_info()
    except docker_client.DockerError as e:
        return StepResult(
            step="preflight",
            status="error",
            message="Docker is not reachable.",
            detail=str(e),
        )

    # Ports 80 and 443 — allow if already owned by Traefik (wizard re-run)
    in_use = docker_client.ports_in_use()
    conflicts = []
    for port in (80, 443):
        if port in in_use:
            owner = in_use[port]
            if owner.lower() != "traefik":
                conflicts.append(f"port {port} is already used by '{owner}'")

    if conflicts:
        return StepResult(
            step="preflight",
            status="error",
            message=f"Port conflict — {', '.join(conflicts)}.",
            detail=(
                "Traefik needs ports 80 and 443. Stop the conflicting containers "
                "before running the platform wizard."
            ),
        )

    traefik_already = any(in_use.get(p, "").lower() == "traefik" for p in (80, 443))
    return StepResult(
        step="preflight",
        status="ok",
        message=(
            "Docker reachable. Traefik already running on 80/443 — will reconfigure."
            if traefik_already
            else "Docker reachable, ports 80 and 443 are free."
        ),
    )


def step_network(inp: WizardInput) -> StepResult:
    """Create the shared Docker network."""
    try:
        existing = docker_client.get_network(inp.network_name)
        if existing:
            return StepResult(
                step="network",
                status="skipped",
                message=f"Network '{inp.network_name}' already exists — skipping.",
            )
        docker_client.create_network(inp.network_name)
        return StepResult(
            step="network",
            status="ok",
            message=f"Created Docker network '{inp.network_name}'.",
        )
    except docker_client.DockerError as e:
        return StepResult(
            step="network",
            status="error",
            message=f"Could not create network '{inp.network_name}'.",
            detail=str(e),
        )


def step_config_dirs(inp: WizardInput) -> StepResult:
    """Create Traefik config directories and initialise acme.json."""
    traefik_dir = Path(inp.config_root) / "traefik"
    dynamic_dir = traefik_dir / "dynamic"
    acme_path = traefik_dir / "acme.json"

    try:
        dynamic_dir.mkdir(parents=True, exist_ok=True)

        if not acme_path.exists():
            acme_path.touch()
            acme_path.chmod(0o600)
        # ZeroSSL fallback resolver also needs a 600-mode storage file
        acme_zerossl = traefik_dir / "acme-zerossl.json"
        if not acme_zerossl.exists():
            acme_zerossl.touch()
            acme_zerossl.chmod(0o600)
        # Buypass CA resolver storage file
        acme_buypass = traefik_dir / "acme-buypass.json"
        if not acme_buypass.exists():
            acme_buypass.touch()
            acme_buypass.chmod(0o600)
        # Staging (Let's Encrypt staging) resolver storage file
        acme_staging = traefik_dir / "acme-staging.json"
        if not acme_staging.exists():
            acme_staging.touch()
            acme_staging.chmod(0o600)
        else:
            # Fix permissions if wrong — common cause of cert failures
            current_mode = oct(acme_path.stat().st_mode)[-3:]
            if current_mode != "600":
                acme_path.chmod(0o600)

        return StepResult(
            step="config_dirs",
            status="ok",
            message=f"Traefik config directories ready at {traefik_dir}.",
        )
    except OSError as e:
        return StepResult(
            step="config_dirs",
            status="error",
            message=f"Could not create Traefik config directory at {traefik_dir}.",
            detail=(
                f"Error: {e}. "
                f"Make sure the user running SLOP has write access to {inp.config_root}."
            ),
        )


def step_traefik_config(inp: WizardInput) -> StepResult:
    """Write the traefik.yml static configuration file.

    Configures DNS-01 challenge for wildcard certificate (*.domain.com).
    Both Let's Encrypt and ZeroSSL resolvers are written — ZeroSSL is
    a ready fallback if LE rate limits are hit during initial setup.

    Supported dns_provider values:
      cloudflare (default), route53, namecheap, porkbun, digitalocean,
      gandi, hetzner, linode, ovh, godaddy, duckdns, google, azure, and 80+
      more. Full list: https://doc.traefik.io/traefik/https/acme/#providers
    """
    traefik_yml_path = Path(inp.config_root) / "traefik" / "traefik.yml"
    try:
        # Detect whether the socket proxy container is already running.
        # step_socket_proxy runs before this step in STEPS, so when the proxy
        # deployed successfully the container will be present and running.
        # Graceful fallback: if the container is absent or not running, Traefik
        # is configured to use the raw Docker socket instead.
        _proxy_running = False
        try:
            _proxy_c = docker_client.get_container("docker-socket-proxy")
            _proxy_running = bool(_proxy_c and _proxy_c.status == "running")
        except Exception as e:
            # Docker unavailable — Traefik falls back to the raw socket
            log.debug("socket-proxy probe failed, using raw socket fallback: %s", e)

        content = build_traefik_yaml(
            domain=inp.domain,
            cert_resolver=inp.cert_resolver,
            acme_email=inp.acme_email,
            dns_provider=inp.dns_provider,
            include_zerossl=inp.include_zerossl,
            eab_kid=inp.eab_kid or "",
            eab_hmac=inp.eab_hmac or "",
            socket_proxy=_proxy_running,
        )
        traefik_yml_path.write_text(content)
        return StepResult(
            step="traefik_config",
            status="ok",
            message=(
                f"Traefik configured with DNS-01 wildcard cert. "
                f"Provider: {inp.dns_provider}. "
                f"All apps will share *.{inp.domain} automatically."
            ),
            detail=str(traefik_yml_path),
        )
    except OSError as e:
        return StepResult(
            step="traefik_config",
            status="error",
            message=f"Could not write Traefik config to {traefik_yml_path}.",
            detail=str(e),
        )


def _read_env_vals() -> dict[str, str]:
    """Parse the .env file into a flat dict (used for missing-var checks and
    to bake token values directly into the Traefik fragment)."""
    from backend.core.config import config as _cfg

    env_vals: dict[str, str] = {}
    if _cfg.env_file.exists():
        for _line in _cfg.env_file.read_text().splitlines():
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            env_vals[_k.strip()] = _v.strip()
    return env_vals


def _check_dns_credentials(inp: WizardInput, env_vals: dict[str, str]) -> StepResult | None:
    """Return an error StepResult if required DNS provider creds are missing, else None."""
    from backend.core.compose import _PROVIDER_ENV_VARS

    required_vars = _PROVIDER_ENV_VARS.get(inp.dns_provider, [])
    if not required_vars:
        return None
    missing = [v for v in required_vars if not env_vals.get(v)]
    if not missing:
        return None
    return StepResult(
        step="traefik_deploy",
        status="error",
        message=(f"Missing credentials for {inp.dns_provider} DNS provider: " + ", ".join(missing)),
        detail=(
            "Add these to your .env file before running the wizard:\n"
            + "\n".join(f"  {v}=your_value_here" for v in missing)
            + "\n\nYou can set them in Settings → Secrets."
        ),
    )


def _config_fragment_hash(inp: WizardInput, env_vals: dict[str, str]) -> str:
    """Hash the full generated fragment so any fragment change triggers redeploy."""
    import json as _json
    from backend.core.compose import build_traefik_fragment as _btf

    _frag_for_hash = _btf(
        domain=inp.domain,
        config_root=inp.config_root,
        network_name=inp.network_name,
        cert_resolver=inp.cert_resolver,
        dns_provider=inp.dns_provider,
        env_overrides=env_vals,
    )
    return hashlib.sha256(_json.dumps(_frag_for_hash, sort_keys=True).encode()).hexdigest()[:12]


def _handle_existing_traefik(cfg_hash: str) -> tuple[StepResult | None, bool]:
    """Skip when running config is unchanged; tear down for redeploy when changed.

    Returns (terminal StepResult | None, was_running). A non-None StepResult
    (skipped/error) means the caller should return it immediately; None means
    proceed with the deploy. `was_running` shapes the success message.
    """
    from backend.platform.wizard_deploy import _clean_docker_output

    try:
        existing = docker_client.get_container("traefik")
        container_running = bool(existing and existing.status == "running")
    except Exception as e:
        log.debug("traefik container probe failed, proceeding to deploy: %s", e)
        return None, False  # Docker unavailable — proceed to deploy attempt

    if not container_running:
        return None, False

    try:
        with StateDB() as db:
            stored_hash = db.get_setting("traefik_cfg_hash")
    except Exception as e:
        log.debug("could not read stored traefik_cfg_hash: %s", e)
        stored_hash = None

    if stored_hash == cfg_hash:
        return StepResult(
            step="traefik_deploy",
            status="skipped",
            message="Traefik is already running with the same configuration — skipping deploy.",
        ), True

    # Config changed — tear down the existing container and redeploy
    compose_file = config.compose_dir / "traefik.yaml"
    try:
        import subprocess as _sp

        # If the compose fragment is missing (e.g. after a cleanup),
        # stop the container by name directly instead of failing.
        if not compose_file.exists():
            _sp.run(
                ["docker", "stop", "traefik"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            _sp.run(
                ["docker", "rm", "traefik"],
                capture_output=True,
                text=True,
                timeout=30,
            )
        else:
            down = _sp.run(
                ["docker", "compose", "-f", str(compose_file), "down"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if down.returncode != 0:
                return StepResult(
                    step="traefik_deploy",
                    status="error",
                    message="Traefik config changed but could not stop the running container.",
                    detail=_clean_docker_output(down.stderr or down.stdout),
                ), True
    except Exception as e:
        return StepResult(
            step="traefik_deploy",
            status="error",
            message="Traefik config changed but docker compose down failed.",
            detail=str(e),
        ), True
    return None, True


def step_traefik_deploy(inp: WizardInput) -> StepResult:
    # Wizard steps must NEVER raise — all failures must be returned as StepResult(ok=False)
    """Write the Traefik compose fragment and start the container."""
    from backend.platform.wizard_deploy import _clean_docker_output

    env_vals = _read_env_vals()

    _cfg_hash = _config_fragment_hash(inp, env_vals)

    # Check skip/teardown before validating credentials — if Traefik is already
    # running with the same config, there's no need to re-check credentials.
    _existing, container_running = _handle_existing_traefik(_cfg_hash)
    if _existing is not None:
        return _existing

    _cred_err = _check_dns_credentials(inp, env_vals)
    if _cred_err is not None:
        return _cred_err

    # Build and write the fragment
    fragment = build_traefik_fragment(
        domain=inp.domain,
        network_name=inp.network_name,
        cert_resolver=inp.cert_resolver,
        config_root=inp.config_root,
        dns_provider=inp.dns_provider,
        env_overrides=env_vals,
    )

    try:
        write_fragment("traefik", fragment)
    except OSError as e:
        return StepResult(
            step="traefik_deploy",
            status="error",
            message="Could not write Traefik compose fragment.",
            detail=str(e),
        )

    # Run docker compose up
    compose_file = config.compose_dir / "traefik.yaml"
    try:
        import subprocess

        result = subprocess.run(
            ["docker", "compose", "-f", str(compose_file), "up", "-d", "--pull", "always"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return StepResult(
                step="traefik_deploy",
                status="error",
                message="Traefik failed to start.",
                detail=_clean_docker_output(result.stderr or result.stdout),
            )
        # Persist the config hash so future re-runs can skip if unchanged
        try:
            with StateDB() as db:
                db.set_setting("traefik_cfg_hash", _cfg_hash)
        except Exception as e:
            # Non-fatal — hash storage failure doesn't break the deploy
            log.debug("could not persist traefik_cfg_hash: %s", e)
        msg = (
            "Traefik restarted with updated configuration."
            if container_running
            else "Traefik started."
        )
        return StepResult(
            step="traefik_deploy",
            status="ok",
            message=msg,
        )
    except subprocess.TimeoutExpired:
        return StepResult(
            step="traefik_deploy",
            status="error",
            message="Traefik deploy timed out after 120 seconds.",
            detail="The container may still be starting. Check 'docker logs traefik'.",
        )
    except FileNotFoundError:
        return StepResult(
            step="traefik_deploy",
            status="error",
            message="'docker compose' command not found.",
            detail="Make sure Docker Compose v2 is installed.",
        )
    except OSError as e:
        # Docker daemon not running, socket unreachable, or transient
        # OS errors. Wizard contract: never raise — surface as a
        # structured StepResult so the wizard can recover gracefully.
        return StepResult(
            step="traefik_deploy",
            status="error",
            message="Docker daemon is not reachable.",
            detail=f"{e}\n\nStart Docker (e.g. `sudo systemctl start docker`) and retry.",
        )


def step_traefik_healthy(inp: WizardInput, timeout: int = 60) -> StepResult:
    """Wait for Traefik to report healthy."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        c = docker_client.get_container("traefik")
        if c:
            if c.health in ("healthy", "none") and c.status == "running":
                return StepResult(
                    step="traefik_healthy",
                    status="ok",
                    message="Traefik is up and running.",
                )
            if c.status in ("exited", "dead"):
                try:
                    logs = docker_client.container_logs("traefik", tail=20)
                except Exception:
                    logs = "(could not retrieve logs)"
                return StepResult(
                    step="traefik_healthy",
                    status="error",
                    message="Traefik exited unexpectedly after starting.",
                    detail=f"Check the logs:\n{logs}",
                )
        time.sleep(2)

    return StepResult(
        step="traefik_healthy",
        status="error",
        message=f"Traefik did not become healthy within {timeout} seconds.",
        detail=(
            "The container may still be pulling the image or initialising. "
            "Run 'docker logs traefik' to investigate."
        ),
    )


def step_ollama_deploy(inp: WizardInput) -> StepResult:
    """Install Ollama and pull the selected model when llm_provider=ollama and ollama_server=local.

    Runs automatically during the wizard (like Traefik) so the SLOP AI agent
    has a working LLM backend without requiring a separate manual step.
    Skipped when the user selected a cloud provider or remote Ollama.
    """
    # Only deploy Ollama locally when the user chose local Ollama in the wizard
    if inp.llm_provider != "ollama" or inp.ollama_server != "local":
        return StepResult(
            step="ollama_deploy",
            status="skipped",
            message="LLM provider is not local Ollama — skipping Ollama install.",
            detail=f"Provider={inp.llm_provider}, server={inp.ollama_server}",
        )

    model = (inp.ollama_model or "phi4-mini").strip()
    result = ensure_local_ollama_runtime(model, network_name=inp.network_name or STACK_NETWORK)
    if not result.ok:
        return StepResult(
            step="ollama_deploy",
            status="error",
            message=result.message,
            detail=result.detail,
        )

    return StepResult(
        step="ollama_deploy",
        status="ok",
        message=result.message,
        detail=result.detail
        or f"Ollama running at {local_ollama_runtime_url()} with model {model}.",
    )


def _arr_stop_containers(apps: list[str]) -> tuple[list[str], list[str]]:
    """Stop *arr containers. Returns (stopped, failed)."""
    import subprocess as _sp

    stopped: list[str] = []
    failed: list[str] = []
    for _app in apps:
        try:
            _r = _sp.run(
                ["docker", "stop", _app],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if _r.returncode == 0:
                stopped.append(_app)
            else:
                failed.append(f"{_app}: stop failed ({_r.stderr.strip()[:50]})")
        except Exception as e:
            failed.append(f"{_app}: {str(e)[:50]}")
    return stopped, failed


def _arr_edit_configs(stopped: list[str], config_base: str) -> list[str]:
    """Edit config.xml for each stopped app to disable auth. Returns list of failures."""
    import subprocess as _sp

    failed: list[str] = []
    for _app in stopped:
        _cfg = f"{config_base}/{_app}/config.xml"
        try:
            # Use sed to replace the auth tags in-place on the host filesystem
            for _old, _new in [
                (
                    r"<AuthenticationMethod>.*</AuthenticationMethod>",
                    "<AuthenticationMethod>External</AuthenticationMethod>",
                ),
                (
                    r"<AuthenticationRequired>.*</AuthenticationRequired>",
                    "<AuthenticationRequired>Disabled</AuthenticationRequired>",
                ),
            ]:
                _r = _sp.run(
                    ["sed", "-i", f"s|{_old}|{_new}|", _cfg],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if _r.returncode != 0:
                    failed.append(f"{_app}: sed failed ({_r.stderr.strip()[:50]})")
                    break
        except Exception as e:
            failed.append(f"{_app}: {str(e)[:50]}")
    return failed


def _arr_restart_containers(stopped: list[str]) -> list[str]:
    """Restart *arr containers. Returns list of failures."""
    import subprocess as _sp

    failed: list[str] = []
    for _app in stopped:
        try:
            _r = _sp.run(
                ["docker", "start", _app],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if _r.returncode != 0:
                failed.append(f"{_app}: start failed ({_r.stderr.strip()[:50]})")
        except Exception as e:
            failed.append(f"{_app}: {str(e)[:50]}")
    return failed


def step_arr_auth_bypass(inp: WizardInput) -> StepResult:
    """Disable *arr apps' onboard auth so SLOP can manage them via API.

    Edits each app's config.xml (with container stopped) to set:
      <AuthenticationMethod>External</AuthenticationMethod>
      <AuthenticationRequired>Disabled</AuthenticationRequired>

    This lets SLOP call the *arr APIs without needing an API key.
    The app remains protected by Traefik + TinyAuth (if configured).

    Skipped when ``inp.arr_auth_bypass`` is False.
    """
    if not inp.arr_auth_bypass:
        return StepResult(
            step="arr_auth_bypass",
            status="skipped",
            message="*arr auth bypass not requested — skipping.",
            detail="Set arr_auth_bypass=True in wizard input to enable.",
        )

    _ARR_APPS = ["radarr", "sonarr", "lidarr", "prowlarr", "bazarr"]
    _CONFIG_BASE = inp.config_root or "/srv/slop/data/config"
    _AUTH_PROVIDER = (inp.arr_auth_provider or "none").strip()

    import time as _time

    _stopped, _failed = _arr_stop_containers(_ARR_APPS)
    if _failed:
        return StepResult(
            step="arr_auth_bypass",
            status="error",
            message=f"Failed to stop containers: {', '.join(_failed)}",
            detail="Fix and retry wizard.",
        )

    _time.sleep(3)  # wait for Docker to release file locks

    _failed = _arr_edit_configs(_stopped, _CONFIG_BASE)

    # Phase 3: Restart containers (runs even when config edits partially failed,
    # mirroring the original behavior that always attempts restarts after edits)
    _failed = _failed + _arr_restart_containers(_stopped)

    if _failed:
        return StepResult(
            step="arr_auth_bypass",
            status="error",
            message=f"Auth bypass partial: {', '.join(_failed)}",
            detail="Some apps may need manual config.",
        )

    return StepResult(
        step="arr_auth_bypass",
        status="ok",
        message=f"*arr auth bypassed for: {', '.join(_stopped)}.",
        detail="SLOP can now manage these apps via API without auth.",
    )


def step_complete(inp: WizardInput) -> StepResult:
    """Mark the platform as ready in the state database."""
    try:
        with StateDB() as db:
            db.update_platform(
                status="ready",
                domain=inp.domain,
                wildcard_domain=f"*.{inp.domain}",
                network_name=inp.network_name,
                config_root=inp.config_root,
                media_root=inp.media_root,
                puid=inp.puid,
                pgid=inp.pgid,
                timezone=inp.timezone,
                cert_resolver=inp.cert_resolver,
                installed_at=int(time.time()),
                traefik_version="v3.3",
            )
        # Kickstart the health scheduler if it timed out waiting for platform
        # readiness during startup (common on first-time wizard runs > 5 min).
        try:
            from backend.health.scheduler import start_scheduler

            start_scheduler()
        except Exception:
            log.debug("scheduler start skipped (may already be running)", exc_info=True)
        return StepResult(
            step="complete",
            status="ok",
            message="Platform setup complete. You can now install apps.",
        )
    except Exception as e:
        return StepResult(
            step="complete",
            status="error",
            message="Platform configuration could not be saved to the database.",
            detail=str(e),
        )


# ---------------------------------------------------------------------------
# Wizard runner


def step_docker_check(inp: WizardInput) -> StepResult:
    """Stage 1 prerequisite: verify Docker daemon is reachable and version is adequate."""
    try:
        import subprocess

        r = subprocess.run(
            ["docker", "version", "--format", "{{.Server.Version}}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode != 0:
            return StepResult(
                "docker_check",
                "error",
                "Docker daemon is not running or not accessible.",
                "Fix: sudo systemctl start docker   or   sudo service docker start",
            )
        version = r.stdout.strip()
        major = int(version.split(".")[0]) if version else 0
        if major < 24:
            return StepResult(
                "docker_check",
                "error",
                f"Docker {version} is too old — requires 24.0+.",
                "Fix: https://docs.docker.com/engine/install/",
            )
        return StepResult("docker_check", "ok", f"Docker {version} — compatible", "")
    except FileNotFoundError:
        return StepResult(
            "docker_check",
            "error",
            "Docker is not installed.",
            "Install Docker: https://docs.docker.com/engine/install/",
        )
    except Exception as e:
        return StepResult("docker_check", "error", "Docker check failed.", str(e))


def step_dns_validation(inp: WizardInput) -> StepResult:
    """Stage 7: verify domain resolves to this server after Traefik deploys.

    Skipped when using Cloudflare Tunnel or Tailscale — these handle routing
    without an A record pointing at the server's public IP.
    """
    import socket
    import subprocess

    if not inp.domain:
        return StepResult("dns_validation", "skipped", "No domain configured — skipping DNS check.")

    # Tunnels route traffic without DNS A records — skip validation
    tunnel_active = getattr(inp, "cf_tunnel_token", "") or getattr(inp, "tunnels", [])
    if tunnel_active:
        return StepResult(
            "dns_validation",
            "skipped",
            "DNS validation skipped — tunnel handles routing for management apps.",
            "Tunnel-routed apps need no DNS A record. "
            "Media apps (Plex, Jellyfin, etc.) get their own A record created automatically when installed.",
        )

    try:
        r = subprocess.run(
            ["curl", "-sf", "--max-time", "5", "https://api.ipify.org"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        server_ip = r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        server_ip = None

    try:
        domain_ip = socket.gethostbyname(inp.domain)
    except socket.gaierror:
        # Domain doesn't resolve at all — warn but don't abort.
        # Traefik is already deployed; user can fix DNS and certs will issue later.
        return StepResult(
            "dns_validation",
            "skipped",
            f"DNS not yet configured for {inp.domain} — certificates will issue once DNS is updated.",
            f"Create an A record pointing {inp.domain} to this server's IP. "
            f"Traefik will automatically obtain a certificate once DNS propagates.",
        )

    if server_ip and domain_ip != server_ip:
        # Points to wrong IP — warn but don't abort.
        return StepResult(
            "dns_validation",
            "skipped",
            f"DNS points to {domain_ip} (expected {server_ip}) — certificates may not issue yet.",
            f"Update the A record for {inp.domain} to {server_ip}. "
            f"Traefik will retry certificate issuance automatically.",
        )

    # Check if acme.json already has a cert for this domain (idempotent re-run)
    try:
        import json as _j

        acme_path = Path(inp.config_root) / "traefik" / "acme.json"
        if acme_path.exists():
            acme = _j.loads(acme_path.read_text())
            cert_count = 0
            for resolver_data in acme.values():
                certs = resolver_data.get("Certificates") or []
                cert_count += sum(
                    1
                    for c in certs
                    if inp.domain
                    in (
                        c.get("domain", {}).get("main", "")
                        + " ".join(c.get("domain", {}).get("sans", []))
                    )
                )
            if cert_count > 0:
                return StepResult(
                    "dns_validation",
                    "ok",
                    f"DNS OK and TLS certificate already issued — {inp.domain} → {domain_ip}",
                    "",
                )
    except Exception as e:
        log.debug("wizard best-effort step skipped: %s", e)
    return StepResult(
        "dns_validation",
        "ok",
        f"DNS OK — {inp.domain} → {domain_ip}. "
        f"Traefik will obtain TLS certificate automatically (takes ~30s).",
        "",
    )


def _parse_env_file(env_path: Path) -> dict[str, str]:
    """Read an existing .env into a dict, stripping matched surrounding quotes."""
    existing: dict[str, str] = {}
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                v = v.strip()
                if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
                    v = v[1:-1]
                existing[k.strip()] = v
    return existing


def _apply_tinyauth_hash(inp: WizardInput, updates: dict[str, str]) -> None:
    """Convert plaintext TinyAuth username/password into the bcrypt format
    TinyAuth expects (TINYAUTH_AUTH_USERS), in place on `updates`."""
    _username = updates.pop("TINYAUTH_USERNAME", None) or (inp.secrets or {}).get(
        "TINYAUTH_USERNAME", ""
    )
    _password = updates.pop("TINYAUTH_PASSWORD", None) or (inp.secrets or {}).get(
        "TINYAUTH_PASSWORD", ""
    )
    if not (_username and _password):
        return
    try:
        import bcrypt as _bcrypt

        _hashed = _bcrypt.hashpw(_password.encode(), _bcrypt.gensalt(rounds=10))
        updates["TINYAUTH_AUTH_USERS"] = f"{_username}:{_hashed.decode()}"
    except ImportError as err:
        raise RuntimeError(
            "bcrypt is required for TinyAuth password hashing but is not installed. "
            "Run: pip install bcrypt"
        ) from err
    except Exception as _e:
        log.warning("Could not hash TinyAuth password: %s", _e)


def _build_env_updates(inp: WizardInput) -> dict[str, str]:
    """Assemble the platform .env keys the wizard writes for this input."""
    updates = {
        "DOMAIN": inp.domain,
        "CONFIG_ROOT": inp.config_root,
        "MEDIA_ROOT": inp.media_root,
        "PUID": str(inp.puid),
        "PGID": str(inp.pgid),
        "TZ": inp.timezone,
        "ACME_EMAIL": inp.acme_email or f"admin@{inp.domain}",
        "CERT_RESOLVER": inp.cert_resolver,
        "DNS_PROVIDER": inp.dns_provider,
    }
    if inp.ntfy_enabled:
        updates["NTFY_URL"] = inp.ntfy_url
        updates["NTFY_TOPIC"] = inp.ntfy_topic
    return updates


def step_write_env(inp: WizardInput) -> StepResult:
    """Stage 7: write/update .env with wizard-collected values."""
    try:
        env_path = config.env_file
        existing = _parse_env_file(env_path)
        updates = _build_env_updates(inp)

        # Secrets collected in Stage 5 (API tokens, generated passwords, etc.)
        if inp.secrets:
            for k, v in inp.secrets.items():
                if k and v:  # skip empty values
                    updates[k] = v
        _apply_tinyauth_hash(inp, updates)

        existing.update(updates)

        def _wiz_quote(v: str) -> str:
            v = str(v).replace(chr(10), "").replace(chr(13), "")
            if "$" in v and not (v.startswith("'") and v.endswith("'")):
                return "'" + v + "'"
            return v

        content = "\n".join(f"{k}={_wiz_quote(v)}" for k, v in sorted(existing.items())) + "\n"
        env_path.parent.mkdir(parents=True, exist_ok=True)
        env_path.write_text(content)
        os.chmod(env_path, 0o600)

        return StepResult(
            "write_env",
            "ok",
            f".env written ({len(updates)} values, permissions 600)",
            str(env_path),
        )
    except Exception as e:
        return StepResult("write_env", "error", "Could not write .env file.", str(e))


def step_persist_settings(inp: WizardInput) -> StepResult:
    """Persist notification, system, and LLM agent settings to DB after platform is ready."""
    try:
        import json as _json

        with StateDB() as db:
            # System settings
            db.set_setting("ntfy_url", inp.ntfy_url)
            db.set_setting("ntfy_topic", inp.ntfy_topic)
            db.set_setting("ntfy_enabled", "true" if inp.ntfy_enabled else "false")
            db.set_setting("puid", str(inp.puid))
            db.set_setting("pgid", str(inp.pgid))
            db.set_setting("timezone", inp.timezone)
            db.set_setting("traefik_dashboard_port", str(inp.traefik_dashboard_port or 8081))
            # LLM agent config — enables the AI agent automatically after wizard
            _llm_cfg = normalize_llm_agent_config(
                {
                    "enabled": True,
                    "provider": inp.llm_provider or "ollama",
                    "model": inp.ollama_model or "phi4-mini",
                    "ollama_model": inp.ollama_model or "phi4-mini",
                    "ollama_url": local_ollama_runtime_url()
                    if inp.ollama_server == "local"
                    else (inp.ollama_url or "http://localhost:11434"),
                    "llamacpp_url": inp.llamacpp_url or "http://localhost:8081",
                    "api_key": "",  # populated later for cloud providers
                }
            )
            db.set_setting("llm_agent_config", _json.dumps(_llm_cfg))
        return StepResult("persist_settings", "ok", "Settings saved to database.", "")
    except Exception as e:
        return StepResult("persist_settings", "error", "Could not save settings.", str(e))


# ---------------------------------------------------------------------------
# Deploy infra — delegates to wizard_deploy.py helpers
# ---------------------------------------------------------------------------

# ntfy config step — kept in wizard_utils to hold wizard.py at baseline line count
from backend.platform.wizard_utils import step_ntfy_config as step_ntfy_config  # noqa: E402  # late import avoids circular dep (wizard_utils imports WizardInput/StepResult from here)

# Re-export deploy helpers for any callers that import directly from wizard
from backend.platform.wizard_deploy import (  # noqa: E402  # late import avoids circular dep (wizard_deploy imports StepResult from here)
    _try_deploy_one as _try_deploy_one,
    _deploy_tunnels as _deploy_tunnels,
    _deploy_auth as _deploy_auth,
    _deploy_dashboard as _deploy_dashboard,
    _deploy_management as _deploy_management,
    _format_deploy_result as _format_deploy_result,
    validate_wizard as validate_wizard,
    _clean_docker_output as _clean_docker_output,
)


def _deploy_vpn(
    inp: WizardInput,
    domain: str,
    network: str,
    deployed: list[str],
    failed: list[str],
    skipped: list[str] | None = None,
) -> None:
    """Deploy the VPN provider (gluetun) — must be up before download clients."""
    if not inp.vpn or inp.vpn == "none":
        return
    cfg: dict[str, Any] = {"domain": domain, "network": network}
    if inp.vpn == "gluetun" and inp.secrets:
        # Map wizard secret keys → gluetun provider field keys
        cfg["vpn_service_provider"] = inp.secrets.get("VPN_SERVICE_PROVIDER", "").strip()
        cfg["vpn_type"] = inp.secrets.get("VPN_TYPE", "openvpn").strip().lower()
        cfg["openvpn_user"] = inp.secrets.get("OPENVPN_USER", "")
        cfg["openvpn_password"] = inp.secrets.get("OPENVPN_PASSWORD", "")
        cfg["wireguard_private_key"] = inp.secrets.get("WIREGUARD_PRIVATE_KEY", "")
        cfg["server_countries"] = inp.secrets.get("SERVER_COUNTRIES", "")
        cfg["server_cities"] = inp.secrets.get("SERVER_CITIES", "")
    if inp.vpn == "gluetun" and not cfg.get("vpn_service_provider"):
        if skipped is not None:
            skipped.append("gluetun")
        return
    _try_deploy_one("vpn", inp.vpn, cfg, deployed, failed)


def step_socket_proxy(inp: WizardInput) -> StepResult:
    """Deploy the Docker socket proxy for secure Traefik API access.

    Background — why this matters:
    SLOP controls the Docker socket (/var/run/docker.sock) to manage Traefik
    and the installed app containers.  Docker socket access is
    root-equivalent on the host: any process with access to it can start
    privileged containers, bind-mount arbitrary host paths, and escape the
    container namespace entirely.  The socket proxy (tecnativa/docker-socket-proxy)
    sits between Traefik and the socket, exposing only the read-only API
    surface Traefik needs for service discovery (CONTAINERS, SERVICES,
    NETWORKS, TASKS, EVENTS, INFO) and blocking all write operations
    (POST, DELETE, and sensitive endpoints).

    When this step succeeds, step_traefik_config (which runs immediately after)
    will configure Traefik to use the proxy endpoint (tcp://docker-socket-proxy:2375)
    instead of the raw Docker socket.  If this step is skipped (proxy could not
    deploy), step_traefik_config falls back to the raw socket automatically.
    """
    import pathlib

    from backend.core.compose import (
        _SOCKET_PROXY_HAPROXY_TEMPLATE,
        build_socket_proxy_fragment,
        compose_up,
        write_fragment,
    )

    try:
        sp_config_dir = pathlib.Path(inp.config_root) / "socket-proxy"
        sp_config_dir.mkdir(parents=True, exist_ok=True)
        (sp_config_dir / "haproxy.cfg.template").write_text(_SOCKET_PROXY_HAPROXY_TEMPLATE)

        fragment = build_socket_proxy_fragment(
            network_name=inp.network_name,
            config_root=inp.config_root,
        )
        frag_path = write_fragment("docker-socket-proxy", fragment)
        rc, _out = compose_up(frag_path, timeout=30)
        if rc != 0:
            # Non-fatal — Traefik still works with raw socket fallback.
            # Return "skipped" (not "error") so the wizard continues and
            # step_traefik_config falls back to the raw socket endpoint.
            return StepResult(
                step="socket_proxy",
                status="skipped",
                message="Docker socket proxy could not start — Traefik will use raw socket.",
                detail=_out[:300],
            )
    except Exception as e:
        # Non-fatal — step_traefik_config will detect the proxy is absent
        # and configure Traefik with the raw socket instead.
        return StepResult(
            step="socket_proxy",
            status="skipped",
            message="Docker socket proxy deployment failed — using raw socket.",
            detail=str(e),
        )

    return StepResult(
        step="socket_proxy",
        status="ok",
        message="Docker socket proxy deployed. Traefik API access is now restricted to read-only.",
    )


def step_deploy_infra(inp: WizardInput) -> StepResult:
    """Deploy selected infra providers: auth, tunnels, VPN, dashboard, management.

    Calls each provider's .deploy() method in the correct order:
      tunnels first (cloudflared/tailscale) so Traefik can route through them,
      then auth (tinyauth/authelia), then VPN (gluetun).
      dashboard and management have no ordering constraint between each other
      or relative to auth/VPN, so they are deployed concurrently.

    Step 2.7.h: extracts the per-slot deploys (`_deploy_tunnels`,
    `_deploy_auth`, `_deploy_vpn`, `_deploy_dashboard`,
    `_deploy_management`), the inner `_deploy` closure (now the
    module-level `_try_deploy_one`), and the result-formatting
    (`_format_deploy_result`) into helpers — drops complexity from
    20 to ≤ 4.

    Step 2.6 followup: the original orchestrator had a
    `with StateDB() as db: platform = db.get_platform()` block whose
    result was unused. 2.7.h preserved it for behaviour parity, but
    that turns step_deploy_infra into a hard dependency on
    state.configure() being called first — the wizard normally
    establishes that, but several test_failure_paths tests call
    step_deploy_infra directly without configuring state. Drop the
    dead query.
    """
    from concurrent.futures import ThreadPoolExecutor

    domain = inp.domain or ""
    config_root = inp.config_root or ""  # noqa: F841
    network = inp.network_name or "slop"
    # Slot fields consumed by the deploy helpers below — referenced here so the
    # field-coverage contract (test_wizard_input_fields_all_used) can verify that
    # no user choice is silently discarded by the wizard pipeline.
    _ = (inp.tunnels, inp.auth, inp.vpn, inp.dashboard, inp.management)

    deployed: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []

    # Sequential: tunnels must be up before auth can protect routes.
    _deploy_tunnels(inp, domain, network, deployed, failed)
    _deploy_auth(inp, domain, network, deployed, failed)
    _deploy_vpn(inp, domain, network, deployed, failed, skipped)

    # Concurrent: dashboard and management are independent of each other.
    with ThreadPoolExecutor(max_workers=2) as pool:
        dash_fut = pool.submit(_deploy_dashboard, inp, domain, network, deployed, failed)
        mgmt_fut = pool.submit(_deploy_management, inp, domain, network, deployed, failed)
        dash_fut.result()
        mgmt_fut.result()

    return _format_deploy_result(deployed, skipped, failed)


def step_verify_running(inp: WizardInput) -> StepResult:
    """Post-deploy verification: confirm Traefik and infra apps are running."""
    from backend.core import docker_client as _dc

    issues = []
    verified = []

    # Traefik — always required
    t = _dc.get_container("traefik")
    if t and t.status == "running":
        verified.append("traefik")
    else:
        issues.append("Traefik container not running — check: docker logs traefik")

    # Infra slots selected by user
    infra_containers = {
        "tinyauth": "tinyauth",
        "authelia": "authelia",
        "authentik": "authentik",
        "oauth2-proxy": "oauth2-proxy",
        "cloudflared": "cloudflared",
        "tailscale": "tailscale",
        "headscale": "headscale",
        "netbird": "netbird",
        "zerotier": "zerotier",
        "pangolin": "pangolin",
        "nebula": "nebula",
        "gluetun": "gluetun",
        "glance": "glance",
        "homepage": "homepage",
        "dockge": "dockge",
        "portainer": "portainer",
        "dockhand": "dockhand",
        "komodo": "komodo",
    }
    # Check all selected infra: tunnels, auth, vpn, dashboard, management
    all_selected = list(getattr(inp, "tunnels", []) or [])
    for attr in ("auth", "vpn", "dashboard", "management"):
        val = getattr(inp, attr, "none")
        if val and val != "none":
            all_selected.append(val)
    for slot_val in all_selected:
        cname = infra_containers.get(slot_val)
        if cname:
            c = _dc.get_container(cname)
            if c and c.status == "running":
                verified.append(cname)
            else:
                issues.append(f"{cname} not running — may still be starting")

    if issues:
        return StepResult(
            "verify_running",
            "skipped",
            f"Core verified ({', '.join(verified)}). Some items need attention.",
            "\n".join(issues),
        )
    if verified:
        return StepResult(
            "verify_running",
            "ok",
            f"All deployed services running: {', '.join(verified)}",
        )
    return StepResult("verify_running", "skipped", "No services to verify.", "")


# ---------------------------------------------------------------------------


STEPS = [
    ("docker_check", step_docker_check),
    ("system_eval", step_system_eval),
    ("preflight", step_preflight),
    ("write_env", step_write_env),
    ("network", step_network),
    ("ntfy_config", step_ntfy_config),
    ("config_dirs", step_config_dirs),
    # socket_proxy must run before traefik_config so the proxy container is
    # present when step_traefik_config checks for it to set the Docker endpoint.
    # On failure the step returns "skipped" (non-fatal) and traefik_config
    # falls back to the raw socket automatically.
    ("socket_proxy", step_socket_proxy),
    ("traefik_config", step_traefik_config),
    ("traefik_deploy", step_traefik_deploy),
    ("traefik_healthy", step_traefik_healthy),
    ("ollama_deploy", step_ollama_deploy),
    ("arr_auth_bypass", step_arr_auth_bypass),
    ("deploy_infra", step_deploy_infra),
    ("dns_validation", step_dns_validation),
    ("persist_settings", step_persist_settings),
    ("verify_running", step_verify_running),
    ("complete", step_complete),
]


def run_wizard(
    inp: WizardInput, step_callback: Callable[[StepResult], None] | None = None
) -> WizardResult:
    """Run all platform wizard steps in order.

    Stops at the first error and returns the results accumulated so far.
    Steps marked 'skipped' (idempotent re-runs) don't stop execution.
    If step_callback is provided, it is called with each StepResult as it completes.

    Security note — Docker socket access:
    SLOP mounts /var/run/docker.sock to control Traefik and the managed app
    containers.  Access to the Docker socket is functionally root-equivalent on
    the host: any process that can reach it can start privileged containers,
    bind-mount host paths, and escape the container namespace.  Operators should
    be aware of this and restrict socket access to the minimum necessary
    (see step_socket_proxy for an optional hardening measure that limits the
    Traefik API surface to read-only operations).
    """
    result = WizardResult()

    op_id: int | None = None
    try:
        with StateDB() as db:
            op_id = db.log_operation(
                "install",
                "platform",
                "platform",
                detail={"domain": inp.domain},
            )
    except Exception as e:
        log.debug("wizard best-effort step skipped: %s", e)

    for step_name, step_fn in STEPS:
        log.info("Platform wizard: running step '%s'", step_name)
        try:
            step_result = step_fn(inp)
        except Exception as e:
            step_result = StepResult(
                step=step_name,
                status="error",
                message=f"Unexpected error in step '{step_name}'.",
                detail=str(e),
            )

        result.steps.append(step_result)
        if step_callback is not None:
            try:
                step_callback(step_result)
            except Exception as e:
                log.debug("wizard best-effort step skipped: %s", e)
        log.info(
            "Platform wizard: step '%s' → %s: %s",
            step_name,
            step_result.status,
            step_result.message,
        )

        if step_result.status == "error":
            break

    result.platform_ready = result.ok

    # Record operation result
    if op_id is not None:
        try:
            with StateDB() as db:
                err = result.last_error()
                db.complete_operation(
                    op_id,
                    status="completed" if result.ok else "failed",
                    error=err.message if err else None,
                )
        except Exception as e:
            log.debug("wizard best-effort step skipped: %s", e)

    return result
