"""Platform-owned local Ollama runtime for the SLOP AI agent.

This is intentionally separate from the catalog app installer. A user's catalog
Ollama app and the platform AI agent runtime are distinct deployment paths.
"""

from __future__ import annotations

import re
import subprocess
import time
from collections.abc import Callable
from typing import Any

import httpx

from backend.core.compose import STACK_NETWORK, compose_pull, compose_up, write_fragment
from backend.core.config import config
from backend.core.logging import get_logger
from backend.infra.base import ProviderResult

log = get_logger(__name__)

OLLAMA_AGENT_SERVICE_KEY = "ollama-agent"
OLLAMA_AGENT_CONTAINER_NAME = "slop-ollama-agent"
OLLAMA_AGENT_PORT = 11434
OLLAMA_AGENT_IMAGE = "ollama/ollama:latest"


def local_ollama_runtime_url() -> str:
    """Return the local Ollama URL from the SLOP process perspective."""
    if config.host_data_dir:
        return f"http://{OLLAMA_AGENT_SERVICE_KEY}:{OLLAMA_AGENT_PORT}"
    return f"http://localhost:{OLLAMA_AGENT_PORT}"


def normalize_llm_agent_config(cfg: dict[str, Any] | None) -> dict[str, Any]:
    """Backfill legacy keys and normalize local Ollama defaults."""
    out = dict(cfg or {})
    provider = (out.get("provider") or "ollama").strip() or "ollama"
    model = (out.get("ollama_model") or out.get("model") or "").strip()
    if model:
        out["model"] = model
        out["ollama_model"] = model
    if provider == "ollama":
        out["ollama_url"] = (out.get("ollama_url") or "").strip() or local_ollama_runtime_url()
    return out


def build_local_ollama_fragment(network_name: str = STACK_NETWORK) -> dict[str, Any]:
    """Compose fragment for the platform-owned local Ollama runtime."""
    data_root = config.effective_data_dir / OLLAMA_AGENT_SERVICE_KEY
    volumes = [f"{data_root}:/root/.ollama"]
    fragment: dict[str, Any] = {
        "image": OLLAMA_AGENT_IMAGE,
        "container_name": OLLAMA_AGENT_CONTAINER_NAME,
        "restart": "unless-stopped",
        "networks": [network_name],
        "environment": {"OLLAMA_HOST": "0.0.0.0"},
        "volumes": volumes,
    }
    # Native-host installs need a loopback bind so the host process can talk to Ollama.
    if not config.host_data_dir:
        fragment["ports"] = [f"127.0.0.1:{OLLAMA_AGENT_PORT}:{OLLAMA_AGENT_PORT}"]
    return fragment


def _emit(
    progress: Callable[..., None] | None,
    *,
    phase: str,
    progress_pct: int,
    message: str,
) -> None:
    if progress is None:
        return
    progress(phase=phase, progress=progress_pct, message=message)


def _api_ready(base_url: str, timeout: int = 3) -> bool:
    try:
        resp = httpx.get(f"{base_url}/api/version", timeout=timeout)
        return resp.status_code == 200
    except Exception as exc:
        log.debug("ollama version probe failed: %s", exc)
        return False


def _model_installed(base_url: str, model: str) -> bool:
    try:
        resp = httpx.get(f"{base_url}/api/tags", timeout=5)
        if resp.status_code != 200:
            return False
        models = resp.json().get("models", [])
        for m in models:
            if not isinstance(m, dict):
                continue
            name = m.get("name") or ""
            if name == model or name == f"{model}:latest":
                return True
        return False
    except Exception as exc:
        log.debug("ollama tags probe failed: %s", exc)
        return False


def ensure_local_ollama_runtime(
    model: str,
    *,
    network_name: str = STACK_NETWORK,
    progress: Callable[..., None] | None = None,
) -> ProviderResult:
    """Ensure the platform-owned Ollama runtime is up and has `model`."""
    model = (model or "phi4-mini").strip() or "phi4-mini"
    base_url = local_ollama_runtime_url()

    _emit(progress, phase="installing", progress_pct=5, message="Preparing local Ollama runtime...")
    if _api_ready(base_url) and _model_installed(base_url, model):
        return ProviderResult.success(
            f"Ollama ready. Model {model} is already available.",
            data={"ollama_url": base_url, "model": model},
        )

    (config.effective_data_dir / OLLAMA_AGENT_SERVICE_KEY).mkdir(parents=True, exist_ok=True)

    # Remove any leftover container from a prior run so compose_up does not
    # hit a container-name conflict (docker compose up requires the name to
    # be free — the --force-recreate flag is not used because it would also
    # tear down unrelated services in the fragment).
    try:
        import subprocess as _sp2

        _sp2.run(
            ["docker", "rm", "-f", OLLAMA_AGENT_CONTAINER_NAME],
            capture_output=True,
            timeout=10,
        )
    except Exception as exc:
        log.debug("pre-flight container rm skipped: %s", exc)

    frag_path = write_fragment(
        OLLAMA_AGENT_SERVICE_KEY,
        build_local_ollama_fragment(network_name=network_name),
        network_name=network_name,
    )

    _emit(progress, phase="installing", progress_pct=10, message="Pulling Ollama runtime image...")
    try:
        rc, out = compose_pull(frag_path, timeout=600)
    except Exception as exc:
        return ProviderResult.failure("Ollama image pull failed.", detail=str(exc))
    if rc != 0:
        return ProviderResult.failure("Ollama image pull failed.", detail=out[:500])

    _emit(progress, phase="installing", progress_pct=25, message="Starting local Ollama runtime...")
    try:
        rc, out = compose_up(frag_path, timeout=120)
    except Exception as exc:
        return ProviderResult.failure("Ollama runtime failed to start.", detail=str(exc))
    if rc != 0:
        return ProviderResult.failure("Ollama runtime failed to start.", detail=out[:500])

    _emit(progress, phase="installing", progress_pct=35, message="Waiting for Ollama API to be ready...")
    for attempt in range(90):
        time.sleep(2)
        if _api_ready(base_url, timeout=5):
            break
        elapsed = (attempt + 1) * 2
        _emit(
            progress,
            phase="installing",
            progress_pct=35 + min(attempt, 30),
            message=f"Waiting for Ollama API... ({elapsed}s elapsed, up to 180s)",
        )
    else:
        return ProviderResult.failure(
            "Ollama started but API not reachable after 180s.",
            detail=(
                f"Check: docker logs {OLLAMA_AGENT_CONTAINER_NAME}\n"
                "Note: first start on GPU-backed hosts can take 2+ minutes."
            ),
        )

    if _model_installed(base_url, model):
        return ProviderResult.success(
            f"Ollama ready. Model {model} is already available.",
            data={"ollama_url": base_url, "model": model},
        )

    _emit(progress, phase="pulling", progress_pct=40, message=f"Pulling model {model}...")
    try:
        proc = subprocess.Popen(
            ["docker", "exec", OLLAMA_AGENT_CONTAINER_NAME, "ollama", "pull", model],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        last_pct = 40
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            match = re.search(r"(\d+)%", line)
            if match:
                last_pct = 40 + int(int(match.group(1)) * 0.55)
            _emit(progress, phase="pulling", progress_pct=last_pct, message=f"Downloading {model}: {line[:80]}")
        proc.wait(timeout=600)
        if proc.returncode != 0:
            return ProviderResult.failure(
                f"Model pull failed for {model}.",
                detail=(
                    f"docker exec {OLLAMA_AGENT_CONTAINER_NAME} ollama pull {model} "
                    f"exited with code {proc.returncode}"
                ),
            )
    except subprocess.TimeoutExpired:
        proc.kill()
        return ProviderResult.failure(
            "Model download timed out after 10 minutes.",
            detail="Try again or pick a smaller model.",
        )
    except Exception as exc:
        return ProviderResult.failure("Model pull error.", detail=str(exc)[:500])

    return ProviderResult.success(
        f"Ollama ready. Model {model} loaded and available.",
        data={"ollama_url": base_url, "model": model},
    )
