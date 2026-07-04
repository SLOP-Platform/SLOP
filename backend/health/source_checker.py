"""backend/health/source_checker.py

Tier 1 — Weekly passive source availability scan.
Tier 2 — On-demand LLM-assisted replacement finder.

Checks:
  - Docker image tags exist at their registry
  - HuggingFace model URLs are reachable (HEAD, no download)
  - GGUF recommended model URLs

Results stored in source_availability table.
Never pulls images, never modifies configs — read-only probes.
"""

from __future__ import annotations

from typing import Any

import asyncio
import json
import re
import time

from backend.core.logging import get_logger

import httpx

log = get_logger(__name__)

SOURCE_CHECK_INTERVAL = 7 * 24 * 3600  # weekly
_REGISTRY_TIMEOUT = 8.0
_HF_TIMEOUT = 10.0


# ---------------------------------------------------------------------------
# Schema migration (called on first use)
# ---------------------------------------------------------------------------


def _ensure_tables() -> None:
    from backend.core.state import StateDB

    with StateDB() as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS source_availability (
                id           INTEGER PRIMARY KEY,
                source_type  TEXT NOT NULL,   -- docker_image | hf_model | catalog_image
                resource_key TEXT NOT NULL,   -- app key or model name
                url          TEXT NOT NULL,   -- full URL or image:tag
                status       TEXT NOT NULL DEFAULT 'unknown',
                              -- ok | missing | unreachable | unknown
                http_status  INTEGER,
                error        TEXT,
                last_checked INTEGER NOT NULL DEFAULT (unixepoch()),
                UNIQUE(source_type, resource_key, url)
            )
        """)


# ---------------------------------------------------------------------------
# Registry probes
# ---------------------------------------------------------------------------

_NAMED_REGISTRIES: frozenset[str] = frozenset(
    {
        "ghcr.io",
        "lscr.io",
        "quay.io",
        "gcr.io",
    }
)


def _parse_image_ref(image: str) -> tuple[str, str]:
    """Split 'repo:tag' → (repo, tag). Default tag is 'latest'.
    The `image.split('/')[-1]` guard avoids treating a port number
    in a hostname (e.g. 'reg.example.com:5000/foo') as a tag."""
    if ":" in image.split("/")[-1]:
        repo, tag = image.rsplit(":", 1)
        return repo, tag
    return image, "latest"


def _resolve_registry(repo: str) -> tuple[str, str]:
    """Resolve (registry_host, repository_name) for a parsed image repo.
    Bare names ('sonarr') route to Docker Hub's `library/` namespace."""
    parts = repo.split("/")
    if parts[0] in _NAMED_REGISTRIES or "." in parts[0]:
        return parts[0], "/".join(parts[1:])
    if "/" in repo:
        return "registry-1.docker.io", repo
    return "registry-1.docker.io", f"library/{repo}"


async def _dockerhub_auth_header(name: str) -> str:
    """Pre-fetch a Docker Hub bearer token (Hub requires one even for
    public images). Returns the full `Bearer <token>` header or empty
    string on any failure — caller proceeds without auth."""
    try:
        async with httpx.AsyncClient(timeout=_REGISTRY_TIMEOUT) as client:
            tr = await client.get(
                f"https://auth.docker.io/token?service=registry.docker.io"
                f"&scope=repository:{name}:pull",
            )
            if tr.status_code == 200:
                return f"Bearer {tr.json().get('token', '')}"
    except Exception:  # noqa: S110  # best-effort Docker auth token fetch; proceed without auth if unavailable
        pass
    return ""


async def _retry_with_bearer_challenge(
    www_auth: str,
    url: str,
    headers: dict[str, str],
) -> tuple[str, int | None, str] | None:
    """Handle the GHCR/LSCR-style 401 + www-authenticate Bearer challenge:
    parse `realm`/`service`/`scope`, fetch a token, retry HEAD with it.
    Returns the result tuple on a successful retry (success or 404), or
    None when the protocol can't be followed (caller falls back to
    reporting the original 401)."""
    if "Bearer" not in www_auth:
        return None
    realm = re.search(r'realm="([^"]+)"', www_auth)
    if not realm:
        return None
    service = re.search(r'service="([^"]+)"', www_auth)
    scope = re.search(r'scope="([^"]+)"', www_auth)
    token_url = realm.group(1)
    params = []
    if service:
        params.append(f"service={service.group(1)}")
    if scope:
        params.append(f"scope={scope.group(1)}")
    if params:
        token_url += "?" + "&".join(params)
    async with httpx.AsyncClient(timeout=_REGISTRY_TIMEOUT) as client:
        tr2 = await client.get(token_url)
        if tr2.status_code != 200:
            return None
        token = tr2.json().get("token", "")
        headers["Authorization"] = f"Bearer {token}"
        r2 = await client.head(url, headers=headers)
    if r2.status_code == 200:
        return "ok", 200, ""
    return ("missing" if r2.status_code == 404 else "unreachable", r2.status_code, "")


async def _check_docker_image(image: str) -> tuple[str, int | None, str]:
    """HEAD the registry manifest for image:tag.
    Returns (status, http_code, error).

    Step 2.7.g: extracts parsing (`_parse_image_ref`), registry
    resolution (`_resolve_registry`), Docker Hub pre-auth
    (`_dockerhub_auth_header`), and the GHCR-style 401 challenge
    handler (`_retry_with_bearer_challenge`) into helpers — drops
    complexity from 20 to ≤ 10.
    """
    repo, tag = _parse_image_ref(image)
    registry, name = _resolve_registry(repo)
    headers = {
        "Accept": "application/vnd.docker.distribution.manifest.v2+json",
    }
    if registry == "registry-1.docker.io":
        auth = await _dockerhub_auth_header(name)
        if auth:
            headers["Authorization"] = auth

    url = f"https://{registry}/v2/{name}/manifests/{tag}"
    try:
        async with httpx.AsyncClient(timeout=_REGISTRY_TIMEOUT) as client:
            r = await client.head(url, headers=headers)
        if r.status_code == 200:
            return "ok", 200, ""
        if r.status_code == 401:
            chal = await _retry_with_bearer_challenge(
                r.headers.get("www-authenticate", ""),
                url,
                headers,
            )
            if chal is not None:
                return chal
            return "missing", r.status_code, f"HTTP {r.status_code}"
        if r.status_code == 404:
            return "missing", 404, f"Image tag not found: {image}"
        return "unreachable", r.status_code, f"HTTP {r.status_code}"
    except httpx.ConnectError:
        return "unreachable", None, "Connection refused"
    except httpx.TimeoutException:
        return "unreachable", None, "Timed out"
    except Exception as e:
        return "unreachable", None, str(e)[:120]


async def _check_hf_url(url: str, hf_token: str = "") -> tuple[str, int | None, str]:
    """HEAD a HuggingFace resolve URL."""
    headers = {"User-Agent": "SLOP/3.0"}
    if hf_token:
        headers["Authorization"] = f"Bearer {hf_token}"
    try:
        async with httpx.AsyncClient(
            timeout=_HF_TIMEOUT,
            follow_redirects=True,
            max_redirects=5,
        ) as client:
            r = await client.head(url, headers=headers)
        if r.status_code in (200, 302, 307):
            return "ok", r.status_code, ""
        if r.status_code == 404:
            return "missing", 404, f"Resource not found: {url}"
        if r.status_code == 401:
            return "missing", 401, "Requires HuggingFace token (gated model)"
        return "unreachable", r.status_code, f"HTTP {r.status_code}"
    except httpx.ConnectError:
        return "unreachable", None, "Connection refused"
    except httpx.TimeoutException:
        return "unreachable", None, "Timed out"
    except Exception as e:
        return "unreachable", None, str(e)[:120]


def _hf_url_from_hf_scheme(hf_url: str) -> str:
    """Convert hf://org/repo/file to https://huggingface.co/org/repo/resolve/main/file"""
    if not hf_url.startswith("hf://"):
        return hf_url
    path = hf_url[5:]  # org/repo/file.gguf
    parts = path.split("/", 2)
    if len(parts) < 3:
        return hf_url
    org, repo, fname = parts
    return f"https://huggingface.co/{org}/{repo}/resolve/main/{fname}"


# ---------------------------------------------------------------------------
# Tier 1 — full weekly scan
# ---------------------------------------------------------------------------


async def run_source_scan() -> dict[str, Any]:
    """Scan all known external sources. Returns a summary dict."""
    _ensure_tables()
    from backend.core.state import StateDB
    from backend.core.gguf_validator import RECOMMENDED_MODELS
    from backend.core.config import config as _cfg

    # Read HF token once
    hf_token = ""
    for line in _cfg.env_file.read_text().splitlines() if _cfg.env_file.exists() else []:
        if "=" in line and not line.strip().startswith("#"):
            k, _, v = line.partition("=")
            if k.strip() == "HF_TOKEN":
                hf_token = v.strip()
                break

    results = {"ok": 0, "missing": 0, "unreachable": 0, "checked": 0}
    tasks: list[tuple[str, str, str]] = []  # (source_type, resource_key, url)

    # 1. Installed app Docker images
    with StateDB() as db:
        apps = db.execute(
            "SELECT key, image, image_tag FROM apps WHERE status NOT IN ('removing','disabled')"
        ).fetchall()

    for app in apps:
        image = f"{app['image']}:{app['image_tag'] or 'latest'}"
        tasks.append(("docker_image", app["key"], image))

    # 2. GGUF recommended model HF URLs
    for m in RECOMMENDED_MODELS:
        hf_url = _hf_url_from_hf_scheme(str(m["hf_url"]))
        tasks.append(("hf_model", str(m["name"]), hf_url))

    # Run all probes concurrently (with limit to avoid hammering)
    sem = asyncio.Semaphore(8)

    async def _probe(source_type: str, resource_key: str, url: str) -> None:
        async with sem:
            if source_type == "docker_image":
                status, code, error = await _check_docker_image(url)
            else:
                status, code, error = await _check_hf_url(url, hf_token)

            results["checked"] += 1
            results[status if status in results else "unreachable"] += 1

            with StateDB() as db:
                db.execute(
                    """
                    INSERT INTO source_availability
                        (source_type, resource_key, url, status, http_status, error, last_checked)
                    VALUES (?, ?, ?, ?, ?, ?, unixepoch())
                    ON CONFLICT(source_type, resource_key, url)
                    DO UPDATE SET
                        status=excluded.status,
                        http_status=excluded.http_status,
                        error=excluded.error,
                        last_checked=unixepoch()
                """,
                    (source_type, resource_key, url, status, code, error),
                )

            if status != "ok":
                log.warning(
                    "Source check %s — %s [%s]: %s %s",
                    status.upper(),
                    resource_key,
                    source_type,
                    code or "",
                    error,
                )

    await asyncio.gather(*[_probe(*t) for t in tasks])

    # Save last scan timestamp
    with StateDB() as db:
        db.set_setting("source_scan_last_at", str(int(time.time())))
        db.set_setting("source_scan_summary", json.dumps(results))

    log.info(
        "Source scan complete: %d checked, %d ok, %d missing, %d unreachable",
        results["checked"],
        results["ok"],
        results["missing"],
        results["unreachable"],
    )
    return results


def due_for_scan() -> bool:
    """Return True if a weekly scan is overdue."""
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            last = db.get_setting("source_scan_last_at")
        return not last or (int(time.time()) - int(last)) > SOURCE_CHECK_INTERVAL
    except Exception:
        return True


# ---------------------------------------------------------------------------
# Tier 2 — LLM replacement finder
# ---------------------------------------------------------------------------


async def find_replacement(
    source_type: str,
    resource_key: str,
    url: str,
) -> dict[str, Any]:
    """Ask the configured LLM to find a replacement for a missing source.

    Returns:
        {
            "suggested_url": str,
            "confidence": float,       # 0.0-1.0
            "reason": str,
            "raw_response": str,
        }
    """
    from backend.core.state import StateDB
    import json as _json

    with StateDB() as db:
        cfg_raw = db.get_setting("llm_agent_config")
    cfg = _json.loads(cfg_raw) if cfg_raw else {}
    provider = cfg.get("provider", "ollama")
    if provider == "llamacpp":
        base_url = cfg.get("llamacpp_url", "http://localhost:8081")
    else:
        base_url = cfg.get("ollama_url", "http://localhost:11434")
    api_key = cfg.get("api_key", "")

    prompt = f"""You are a homelab software assistant. A resource URL has returned 404 or is missing.

Resource type: {source_type}
Resource name: {resource_key}
Failed URL: {url}

Your task:
1. Identify what this resource is (Docker image, HuggingFace model file, etc.)
2. Determine the most likely current canonical URL or location
3. Return ONLY a JSON object with these fields:
   - suggested_url: the new URL (empty string if unknown)
   - confidence: 0.0 to 1.0 (how confident you are this is correct)
   - reason: one sentence explaining what changed and why this is the right URL

For Docker images: check if the image was renamed, moved to a new org, or if there is a maintained fork.
For HuggingFace models: check if the repo was renamed (common when vendors add org prefixes like microsoft_, meta_, etc.) or if the quantizer moved the files.

Respond ONLY with the JSON object, no other text."""

    headers = {"Content-Type": "application/json"}
    timeout = 30.0

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            if provider == "ollama":
                from backend.core.llm_router import best_model_for

                rec = best_model_for("reasoning")
                model = (
                    (rec.ollama_name or rec.filename.replace(".gguf", "")) if rec else "phi4-mini"
                )
                resp = await client.post(
                    f"{base_url}/api/generate",
                    json={"model": model, "prompt": prompt, "stream": False, "format": "json"},
                )
                raw = resp.json().get("response", "{}")
            else:
                # Cloud providers
                _cloud_urls = {
                    "groq": "https://api.groq.com/openai/v1/chat/completions",
                    "cerebras": "https://api.cerebras.ai/v1/chat/completions",
                    "openrouter": "https://openrouter.ai/api/v1/chat/completions",
                    "nim": "https://integrate.api.nvidia.com/v1/chat/completions",
                    "gai": "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions",
                    "llamacpp": f"{base_url}/v1/chat/completions",
                    "shimmy": f"{base_url}/v1/chat/completions",
                    "localai": f"{base_url}/v1/chat/completions",
                }
                _defaults = {
                    "groq": "llama-3.3-70b-versatile",
                    "cerebras": "llama-3.3-70b",
                    "openrouter": "meta-llama/llama-3.3-70b-instruct:free",
                    "gai": "gemini-2.0-flash",
                }
                model = cfg.get("ollama_model") or _defaults.get(provider, "")
                call_url = _cloud_urls.get(provider, f"{base_url}/v1/chat/completions")
                hdrs = dict(headers)
                if api_key:
                    hdrs["Authorization"] = f"Bearer {api_key}"
                resp = await client.post(
                    call_url,
                    headers=hdrs,
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": prompt}],
                        "response_format": {"type": "json_object"},
                    },
                )
                raw = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "{}")

        data = json.loads(raw.strip())
        return {
            "suggested_url": data.get("suggested_url", ""),
            "confidence": float(data.get("confidence", 0.0)),
            "reason": data.get("reason", ""),
            "raw_response": raw,
        }

    except Exception as e:
        log.warning("find_replacement LLM call failed: %s", e)
        return {
            "suggested_url": "",
            "confidence": 0.0,
            "reason": f"LLM unavailable: {e}",
            "raw_response": "",
        }
