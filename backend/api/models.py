"""backend/api/models.py

Model management API routes.

GET  /api/models/gguf                    — list GGUF files in models directory
GET  /api/models/recommended             — curated list with HuggingFace URLs
POST /api/models/gguf/validate           — validate an existing file path
POST /api/models/gguf/download           — start a download (SSE stream)
DELETE /api/models/gguf/{filename}       — remove a GGUF file

GET  /api/models/agent/config            — current agent config (backend + model)
POST /api/models/agent/config            — set agent backend and model
POST /api/models/agent/evaluate          — run evaluation prompt on current model
"""

from __future__ import annotations

from typing import Any

import asyncio
import json
import time
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request

from backend.api.rate_limit import limiter
from fastapi.responses import StreamingResponse

# Request/response DTOs extracted to models_schemas.py (#1302 linecount drain).
# Re-exported (redundant-alias idiom) so response_model= refs, handler bodies,
# and any `from backend.api.models import <Schema>` callers resolve unchanged.
from backend.api.models_schemas import (
    AgentConfig as AgentConfig,
    DownloadRequest as DownloadRequest,
    EvaluateResult as EvaluateResult,
    FixRecord as FixRecord,
    GGUFFileInfo as GGUFFileInfo,
    HardwareEvalResult as HardwareEvalResult,
    HardwareEvalStep as HardwareEvalStep,
    ModelRegistryEntry as ModelRegistryEntry,
    PreflightResult as PreflightResult,
    RegistryUpdateRequest as RegistryUpdateRequest,
    ValidateRequest as ValidateRequest,
    ValidateResponse as ValidateResponse,
)

from backend.core.config import config
from backend.core.gguf_validator import (
    RECOMMENDED_MODELS,
    download_gguf,
    list_gguf_files,
    validate_gguf,
)
from backend.core.error_detail import safe_detail
from backend.core.logging import get_logger
from backend.core.state import StateDB
from backend.core.url_guard import assert_not_metadata_url
from backend.core.url_guard_httpx import pinned_async_client

log = get_logger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# SSRF guard — H-10
# ---------------------------------------------------------------------------


def _validate_gguf_url(url: str) -> str:
    """Raise ValueError if url is not a safe https:// or hf:// URL.

    Delegates to the shared SSRF guard (``backend/core/url_guard``) rather than a
    bespoke duplicate (#1151): it rejects non-https schemes, private/loopback/
    link-local IP literals (incl. alternate numeric encodings + CGNAT that the
    old bespoke list missed), AND — via ``resolve_dns`` — a hostname that resolves
    to an internal address (DNS-rebinding-to-private, #1102). ``hf://`` shorthand
    is resolved later by ``resolve_gguf_url``. ``UrlNotAllowed`` is a
    ``ValueError`` subclass, so existing ``except ValueError`` callers are unchanged.
    """
    if url.startswith("hf://"):
        return url  # handled by resolve_gguf_url in gguf_validator
    from backend.core.url_guard import assert_allowed_url

    assert_allowed_url(url, allowed_hosts=None, resolve_dns=True)
    return url


# Models directory — inside data_dir so it persists
def _read_hf_token() -> str:
    """Read HF_TOKEN from the .env file, if present."""
    from backend.core.config import config as _cfg

    if not _cfg.env_file.exists():
        return ""
    for line in _cfg.env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == "HF_TOKEN":
            return v.strip()
    return ""


def _models_dir() -> Path:
    d = config.data_dir / "models"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------


# Diagnostic test prompt — contains a planted error
_EVAL_PROMPT = """You are a homelab monitoring agent. Analyze this container health report and respond with a JSON object only.

Container: sonarr
Status: unhealthy
Recent logs:
  [2026-04-28 14:23:01] INFO  Starting Sonarr...
  [2026-04-28 14:23:05] ERROR Cannot open database file: /config/sonarr.db: database is locked
  [2026-04-28 14:23:05] ERROR Failed to initialize NzbDrone.Core.Datastore.Database
  [2026-04-28 14:23:06] FATAL Unhandled exception: database is locked

Respond with only this JSON structure, no other text:
{
  "problem": "one sentence description",
  "cause": "one sentence root cause",
  "suggested_fix": "specific action to take",
  "action": "restart" or "config_change" or "manual" or "escalate",
  "confidence": 0.0 to 1.0
}"""

_EXPECTED_ERROR = "database"  # must appear in problem or cause


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/gguf", response_model=list[GGUFFileInfo])
def list_models() -> list[GGUFFileInfo]:
    """List GGUF model files.

    Sources (merged, deduplicated by filename):
    1. SLOP data/models/*.gguf  — downloaded through the UI
    2. Ollama API /api/tags           — models pulled directly via ollama pull
    """
    import urllib.request as _req
    from backend.core.state import StateDB as _SDB
    import json as _json

    # Source 1: local GGUF files
    local_files = {f["filename"]: f for f in list_gguf_files(_models_dir())}

    # Source 2: Ollama-managed models
    try:
        with _SDB() as db:
            cfg_raw = db.get_setting("llm_agent_config")
        cfg = _json.loads(cfg_raw) if cfg_raw else {}
        ollama_url = cfg.get("ollama_url", "http://localhost:11434")
        if not ollama_url.startswith(("http://", "https://")):
            raise ValueError(f"Unsupported Ollama URL scheme: {ollama_url}")
        assert_not_metadata_url(ollama_url, resolve_dns=False)  # SSRF floor #1193 (caught below)

        req = _req.Request(  # noqa: S310  # scheme validated above
            f"{ollama_url}/api/tags",
            headers={"User-Agent": "SLOP/3.0"},
        )
        with _req.urlopen(req, timeout=3) as resp:  # noqa: S310  # nosec B310  # scheme validated above
            data = _json.loads(resp.read())
        for m in data.get("models", []):
            name = m.get("name", "")
            size_bytes = m.get("size", 0)
            # Use name as filename key; mark as ollama-managed
            key = name
            if key not in local_files:
                local_files[key] = {
                    "filename": name,
                    "path": f"ollama://{name}",
                    "size_mb": round(size_bytes / 1024 / 1024, 1),
                    "valid": True,
                    "gguf_version": None,
                    "error": None,
                    "warning": "Managed by Ollama (not a local GGUF file)",
                }
    except Exception:  # noqa: S110  # Ollama unreachable or misconfigured — only show local files
        pass

    return [GGUFFileInfo(**f) for f in local_files.values()]


@router.get("/recommended")
def recommended_models() -> list[dict[str, Any]]:
    """Return the curated list of recommended models with download URLs."""
    return RECOMMENDED_MODELS


@router.post("/gguf/validate", response_model=ValidateResponse)
def validate_model_file(req: ValidateRequest) -> ValidateResponse:
    """Validate an existing file on the server filesystem.

    Path is restricted to the configured GGUF models directory to prevent
    directory traversal attacks — callers cannot read arbitrary files.
    """
    models_dir = _models_dir().resolve()
    # Resolve + containment-check together: Path(...).resolve() itself raises ValueError on
    # an invalid path (e.g. an embedded null byte), which must surface as a 400 like a
    # traversal attempt — not an uncaught 500 (#1197). relative_to raises the same type.
    try:
        requested = Path(req.path).resolve()
        requested.relative_to(models_dir)
    except ValueError as e:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Path must be within the models directory ({models_dir}). "
                f"Provide a filename only, e.g. 'phi4-mini.gguf'."
            ),
        ) from e
    result = validate_gguf(requested)
    return ValidateResponse(
        valid=result.valid,
        size_mb=round(result.file_size_mb, 1),
        gguf_version=result.gguf_version,
        error=result.error,
        warning=result.warning,
    )


@router.get("/gguf/download")
def download_model_sse(
    url: str,
    filename: str = "",
) -> StreamingResponse:
    """Stream-download a GGUF file via SSE (GET for EventSource compatibility).

    EventSource requires GET. Events emitted:
      progress: { bytes_downloaded, total_bytes, percent, mb_downloaded, total_mb }
      complete: { valid, filename, size_mb, gguf_version, error, warning }
      error:    { error }

    The client opens EventSource and closes it on 'complete' or 'error'.
    Progress events fire every ~512KB of download progress.
    """
    import queue
    import threading

    # SSRF guard — H-10: reject non-https, private IPs, file:// etc.
    try:
        _validate_gguf_url(url)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=safe_detail(exc, "Invalid or disallowed model URL.", log=log)
        ) from exc

    event_queue: queue.Queue[Any] = queue.Queue()

    hf_token = _read_hf_token()

    def _download_thread() -> None:
        try:
            gen = download_gguf(url, _models_dir(), filename or None, hf_token=hf_token)
            for progress in gen:
                event_queue.put(("progress" if not progress.done else "done_progress", progress))
        except Exception as e:
            event_queue.put(("thread_error", str(e)))
        finally:
            event_queue.put(("__end__", None))

    threading.Thread(target=_download_thread, daemon=True).start()

    async def _stream() -> Any:
        loop = asyncio.get_event_loop()

        while True:
            try:
                event_type, data = await loop.run_in_executor(
                    None, lambda: event_queue.get(timeout=120)
                )
            except Exception:
                yield f"event: error\ndata: {json.dumps({'error': 'Download timed out.'})}\n\n"
                return

            if event_type == "__end__":
                return

            if event_type == "thread_error":
                yield f"event: error\ndata: {json.dumps({'error': str(data)})}\n\n"
                return

            progress = data
            if progress.error:
                yield f"event: error\ndata: {json.dumps({'error': progress.error})}\n\n"
                return

            if progress.done:
                # Validate and emit complete event
                fname = filename or url.split("/")[-1].split("?")[0]
                dest = _models_dir() / fname
                result = validate_gguf(dest) if dest.exists() else None
                payload = {
                    "valid": result.valid if result else False,
                    "filename": fname,
                    "size_mb": round(result.file_size_mb, 1) if result else 0,
                    "gguf_version": result.gguf_version if result else None,
                    "error": result.error if result else "Download incomplete",
                    "warning": result.warning if result else None,
                }
                yield f"event: complete\ndata: {json.dumps(payload)}\n\n"
                return
            else:
                # Real-time progress
                total_mb = (
                    round(progress.total_bytes / 1024 / 1024, 1) if progress.total_bytes else None
                )
                payload = {
                    "bytes_downloaded": progress.bytes_downloaded,
                    "total_bytes": progress.total_bytes,
                    "percent": round(progress.percent, 1) if progress.percent else 0,
                    "mb_downloaded": round(progress.mb_downloaded, 1),
                    "total_mb": total_mb,
                }
                yield f"event: progress\ndata: {json.dumps(payload)}\n\n"

    return StreamingResponse(
        _stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/gguf/download")
@limiter.limit("5/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped; heavy external GGUF download (#1205 external-fetch tier)
def download_model(request: Request, req: DownloadRequest) -> dict[str, Any]:
    """Start a GGUF download (POST version for non-EventSource clients).

    For browser UI use GET /gguf/download?url=...&filename=... with EventSource.
    This POST version is for programmatic/CLI use; it blocks until complete.
    """
    # SSRF guard — H-10: reject non-https, private IPs, file:// etc.
    try:
        _validate_gguf_url(req.url)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=safe_detail(exc, "Invalid or disallowed model URL.", log=log)
        ) from exc

    fname = req.filename or req.url.split("/")[-1].split("?")[0]
    dest = _models_dir() / fname
    try:
        gen = download_gguf(req.url, _models_dir(), req.filename, hf_token=_read_hf_token())
        for _ in gen:
            pass  # consume generator
        result = validate_gguf(dest) if dest.exists() else None
        return {
            "ok": result.valid if result else False,
            "filename": fname,
            "size_mb": round(result.file_size_mb, 1) if result else 0,
            "error": result.error if result else None,
        }
    except Exception as e:
        return {
            "ok": False,
            "filename": fname,
            "error": safe_detail(e, "Validation failed.", log=log),
        }


@router.delete("/gguf/{filename}")
def delete_model(filename: str) -> dict[str, Any]:
    """Remove a GGUF model file from the models directory."""
    # Safety: only allow filenames, no path traversal
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename.")

    path = _models_dir() / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"Model file '{filename}' not found.")

    path.unlink()
    from backend.core.llm_router import remove_model_from_registry

    remove_model_from_registry(filename)
    log.info("Deleted model: %s", filename)
    return {"message": f"Model '{filename}' deleted."}


# ---------------------------------------------------------------------------
# Agent configuration
# ---------------------------------------------------------------------------


_AGENT_CONFIG_KEY = "llm_agent_config"


@router.get("/agent/config", response_model=AgentConfig)
def get_agent_config() -> AgentConfig:
    """Return the current LLM agent configuration."""
    with StateDB() as db:
        raw = db.get_setting(_AGENT_CONFIG_KEY)
    if raw:
        try:
            return AgentConfig(**json.loads(raw))
        except Exception:  # noqa: S110  # corrupted config DB entry; fall back to defaults
            pass
    return AgentConfig()


@router.post("/agent/config", response_model=AgentConfig)
def set_agent_config(cfg: AgentConfig) -> AgentConfig:
    """Update the LLM agent configuration."""
    if cfg.backend not in ("ollama", "llamacpp"):
        raise HTTPException(
            status_code=422,
            detail="backend must be 'ollama' or 'llamacpp'",
        )
    if cfg.confidence_threshold < 0.5 or cfg.confidence_threshold > 1.0:
        raise HTTPException(
            status_code=422,
            detail="confidence_threshold must be between 0.5 and 1.0",
        )
    with StateDB() as db:
        db.set_setting(_AGENT_CONFIG_KEY, cfg.model_dump_json())
    return cfg


@router.post("/agent/evaluate", response_model=EvaluateResult)
@limiter.limit("5/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped; LLM inference external call, heaviest (#1205 external-fetch tier)
async def evaluate_model(request: Request) -> EvaluateResult:
    """Run a diagnostic evaluation prompt on the current agent model.

    Tests whether the configured model:
      1. Responds within a reasonable time
      2. Returns parseable JSON
      3. Correctly identifies a planted database-lock error

    Returns a structured score: pass / warn / fail.
    """
    with StateDB() as db:
        raw = db.get_setting(_AGENT_CONFIG_KEY)
    cfg = AgentConfig(**json.loads(raw)) if raw else AgentConfig()

    start = time.monotonic()
    raw_response = ""
    parsed = {}

    try:
        # SSRF floor (#1193): ollama_url/llamacpp_url are operator-settable (caught below).
        eval_url = cfg.ollama_url if cfg.backend == "ollama" else cfg.llamacpp_url
        assert_not_metadata_url(eval_url, resolve_dns=False)
        if cfg.backend == "ollama":
            async with pinned_async_client(timeout=90) as client:
                resp = await client.post(
                    f"{cfg.ollama_url}/api/generate",
                    json={
                        "model": cfg.ollama_model,
                        "prompt": _EVAL_PROMPT,
                        "stream": False,
                        "format": "json",
                    },
                )
                resp.raise_for_status()
                raw_response = resp.json().get("response", "")

        else:  # llamacpp
            async with pinned_async_client(timeout=90) as client:
                resp = await client.post(
                    f"{cfg.llamacpp_url}/completion",
                    json={
                        "prompt": _EVAL_PROMPT,
                        "n_predict": 256,
                        "temperature": 0.1,
                        "stop": ["\n\n"],
                    },
                )
                resp.raise_for_status()
                raw_response = resp.json().get("content", "")

    except Exception as e:
        elapsed = time.monotonic() - start
        return EvaluateResult(
            passed=False,
            backend=cfg.backend,
            model=cfg.ollama_model if cfg.backend == "ollama" else cfg.llamacpp_model_file,
            inference_seconds=round(elapsed, 1),
            parsed_correctly=False,
            identified_error=False,
            response_preview=str(e)[:200],
            score="fail",
            reason=f"Inference failed: {e}",
        )

    elapsed = time.monotonic() - start
    model_id = cfg.ollama_model if cfg.backend == "ollama" else cfg.llamacpp_model_file

    # Try to parse JSON
    parsed_ok = False
    identified = False
    try:
        # Strip markdown fences if model wrapped in them
        clean = raw_response.strip()
        if clean.startswith("```"):
            clean = "\n".join(clean.split("\n")[1:])
            clean = clean.rstrip("`").strip()
        parsed = json.loads(clean)
        parsed_ok = all(k in parsed for k in ("problem", "cause", "action", "confidence"))
        text_lower = (str(parsed.get("problem", "")) + str(parsed.get("cause", ""))).lower()
        identified = _EXPECTED_ERROR in text_lower
    except json.JSONDecodeError:
        pass

    # Score
    TIME_WARN = 30  # seconds
    TIME_FAIL = 60

    if not parsed_ok:
        score, reason = "fail", "Response was not valid JSON with required fields."
    elif not identified:
        score, reason = "warn", "Model did not correctly identify the database error in the logs."
    elif elapsed > TIME_FAIL:
        score, reason = "warn", f"Inference took {elapsed:.0f}s — too slow for real-time use."
    elif elapsed > TIME_WARN:
        score, reason = "warn", f"Inference took {elapsed:.0f}s — usable but slow on this hardware."
    else:
        score, reason = "pass", None

    return EvaluateResult(
        passed=(score == "pass"),
        backend=cfg.backend,
        model=model_id,
        inference_seconds=round(elapsed, 1),
        parsed_correctly=parsed_ok,
        identified_error=identified,
        response_preview=raw_response[:300],
        score=score,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Full hardware evaluation endpoint
# ---------------------------------------------------------------------------


@router.get("/evaluate-hardware")
@router.post("/evaluate-hardware")
def evaluate_hardware_for_model(
    model_size_gb: float = 0.0, quantization: str = "Q4_K_M"
) -> HardwareEvalResult:
    """Full hardware capability scan — benchmarks RAM/CPU/GPU/storage,
    then checks which recommended models can run on this system."""
    from backend.core.system_eval import (
        evaluate_system as _eval_system,
        detect_avx,
        detect_gpu_extended,
        benchmark_ram_bandwidth_gb,
        benchmark_cpu_gflops,
        benchmark_storage_mb_s,
        evaluate_model_compatibility,
    )
    from backend.core.gguf_validator import RECOMMENDED_MODELS
    import shutil

    steps: list[HardwareEvalStep] = []

    # System profile + benchmarks (run in parallel feel via sequential — fast enough)
    profile = _eval_system()
    avx = detect_avx()
    gpu = detect_gpu_extended()
    ram_bw = benchmark_ram_bandwidth_gb()
    cpu_gf = benchmark_cpu_gflops()
    storage_mb_s = benchmark_storage_mb_s(str(config.data_dir.parent))

    # Use largest recommended model as default test size if none specified
    if model_size_gb <= 0:
        model_size_gb = max(float(m["size_gb"]) for m in RECOMMENDED_MODELS)  # type: ignore[arg-type]  # RECOMMENDED_MODELS values are float at runtime; root-cause typing in 1.2.c scope

    # RAM capacity + bandwidth
    available_gb = profile.available_ram_gb
    total_gb = profile.total_ram_gb
    bw_label = f"{ram_bw} GB/s" if ram_bw > 0 else "unknown speed"
    ram_detail = f"{available_gb:.1f} GB free of {total_gb:.1f} GB · {bw_label}"
    ram_status = "ok" if available_gb >= 4 else ("warn" if available_gb >= 2 else "error")
    steps.append(HardwareEvalStep(label="RAM", status=ram_status, detail=ram_detail))

    # CPU + speed
    gf_label = f"{cpu_gf} GFLOPS" if cpu_gf > 0 else ""
    cpu_detail = f"{profile.cpu_cores} cores"
    if profile.cpu_model:
        cpu_detail += f" ({profile.cpu_model[:40]})"
    if avx.get("avx2"):
        cpu_detail += " · AVX2 ✓"
    else:
        cpu_detail += " · no AVX2 (llama.cpp needs this)"
    if avx.get("avx512"):
        cpu_detail += " · AVX512 ✓"
    if gf_label:
        cpu_detail += f" · {gf_label}"
    cpu_status = "ok" if avx.get("avx2") else "warn"
    steps.append(HardwareEvalStep(label="CPU", status=cpu_status, detail=cpu_detail))

    # GPU
    if gpu.get("name"):
        vram_gb = gpu.get("vram_mb", 0) / 1024
        gpu_detail = f"{gpu['name']}"
        if vram_gb > 0:
            gpu_detail += f", {vram_gb:.1f} GB VRAM"
        if gpu.get("cuda_version"):
            gpu_detail += f", CUDA {gpu['cuda_version']}"
        if gpu.get("inference_capable") and vram_gb >= model_size_gb:
            gpu_detail += " — model fits in VRAM ✓"
            gpu_status = "ok"
        elif gpu.get("inference_capable"):
            gpu_detail += " — CPU offloading needed"
            gpu_status = "warn"
        else:
            gpu_status = "info"
        steps.append(HardwareEvalStep(label="GPU", status=gpu_status, detail=gpu_detail))
    else:
        steps.append(
            HardwareEvalStep(
                label="GPU", status="info", detail="No GPU detected — CPU-only inference"
            )
        )

    # Storage + speed
    try:
        disk = shutil.disk_usage(str(config.data_dir))
        free_gb = disk.free / 1024**3
        spd_label = f" · {storage_mb_s:.0f} MB/s write" if storage_mb_s > 0 else ""
        st_status = "ok" if free_gb >= 10 else ("warn" if free_gb >= 2 else "error")
        steps.append(
            HardwareEvalStep(
                label="Storage", status=st_status, detail=f"{free_gb:.1f} GB free{spd_label}"
            )
        )
        storage_free_gb = free_gb
    except Exception:
        storage_free_gb = 999.0
        steps.append(
            HardwareEvalStep(label="Storage", status="info", detail="Could not check disk")
        )

    # Per-model compatibility table (replaces fixed size check)
    gpu_vram_gb = gpu.get("vram_mb", 0) / 1024
    gpu_capable = gpu.get("inference_capable", False)
    model_rows = []
    for m in RECOMMENDED_MODELS:
        sz = float(m["size_gb"])  # type: ignore[arg-type]  # RECOMMENDED_MODELS values are float at runtime; root-cause typing in 1.2.c scope
        ram_ok = available_gb >= sz * 1.05
        gpu_ok = gpu_capable and gpu_vram_gb >= sz
        if gpu_ok:
            mode = "GPU ✓"
            row_status = "ok"
        elif ram_ok:
            mode = "CPU"
            row_status = "ok" if avx.get("avx2") else "warn"
        else:
            mode = "✗ insufficient RAM"
            row_status = "error"
        model_rows.append(
            {
                "name": m["name"],
                "size_gb": sz,
                "recommended_for": m["recommended_for"],
                "mode": mode,
                "status": row_status,
            }
        )
    steps.append(
        HardwareEvalStep(
            label="Model compatibility",
            status="info",
            detail=str(model_rows),  # serialised — UI picks this apart
        )
    )

    # Quantization advice
    quant_rec = quantization
    if gpu.get("inference_capable") and gpu.get("vram_mb", 0) / 1024 >= model_size_gb:
        quant_rec = "Q8_0"
        quant_detail = "GPU available — use Q8_0 for best quality"
    elif available_gb >= model_size_gb * 1.5:
        quant_rec = "Q5_K_M"
        quant_detail = "Good RAM headroom — Q5_K_M for better quality"
    else:
        quant_rec = "Q4_K_M"
        quant_detail = "RAM-constrained — Q4_K_M recommended"
    steps.append(HardwareEvalStep(label="Quantization", status="info", detail=quant_detail))

    # Full compat check
    compat = evaluate_model_compatibility(
        model_size_gb=model_size_gb,
        quantization=quant_rec,
        system_ram_gb=profile.total_ram_gb,
        available_ram_gb=available_gb,
        cpu_cores=profile.cpu_cores,
        avx2=avx.get("avx2", False),
        gpu=gpu,
        storage_free_gb=storage_free_gb,
    )

    # Verdict step
    verdict_status = {"runs_well": "ok", "runs_slowly": "warn", "cannot_run": "error"}.get(
        compat["verdict"], "info"
    )
    steps.append(HardwareEvalStep(label="Verdict", status=verdict_status, detail=compat["summary"]))

    return HardwareEvalResult(
        steps=steps,
        verdict=compat["verdict"],
        summary=compat["summary"],
        recommended_quantization=quant_rec,
        estimated_tokens_per_second=compat["estimated_tokens_per_second"],
        inference_mode=compat["inference_mode"],
    )


# ---------------------------------------------------------------------------
# Pre-download URL validation
# ---------------------------------------------------------------------------


@router.post("/gguf/preflight")
@limiter.limit("10/minute")  # type: ignore[untyped-decorator]  # slowapi decorator is untyped; HEAD probe of user URL (#1205 external-fetch tier)
def preflight_download(request: Request, url: str) -> PreflightResult:
    """HEAD request to validate a model URL before starting download.

    Detects: 401 (HuggingFace gated model), 404 (bad URL),
    wrong content-type (not a binary file), file size.
    """
    import urllib.error as _uerr

    from backend.core.url_guard import pinned_urlopen

    # SSRF guard — H-10: reject non-https, private IPs, file:// etc.
    try:
        _validate_gguf_url(url)
    except ValueError as exc:
        raise HTTPException(
            status_code=400, detail=safe_detail(exc, "Invalid or disallowed model URL.", log=log)
        ) from exc

    # Build headers
    headers = {"User-Agent": "SLOP/3.0"}

    # Add HF token if present
    hf_token = _read_hf_token()
    if hf_token and "huggingface.co" in url:
        headers["Authorization"] = f"Bearer {hf_token}"

    fname = url.split("/")[-1].split("?")[0]

    try:
        # SSRF-hardened seam: https-only + connect-time IP pin (re-validated per hop),
        # closing the residual rebinding TOCTOU between _validate_gguf_url and the fetch.
        with pinned_urlopen(url, headers=headers, method="HEAD", timeout=15) as resp:
            size_bytes = resp.headers.get("Content-Length")
            content_type = resp.headers.get("Content-Type", "")
            size_mb = round(int(size_bytes) / 1024 / 1024, 1) if size_bytes else None
            return PreflightResult(
                ok=True,
                filename=fname,
                size_mb=size_mb,
                content_type=content_type,
                requires_auth=False,
                error=None,
            )
    except _uerr.HTTPError as e:
        if e.code == 401:
            return PreflightResult(
                ok=False,
                filename=fname,
                size_mb=None,
                content_type=None,
                requires_auth=True,
                error=(
                    "This model requires a HuggingFace token. "
                    "Add HF_TOKEN to Settings → Secrets. "
                    "Get your token at huggingface.co/settings/tokens"
                ),
            )
        if e.code == 404:
            return PreflightResult(
                ok=False,
                filename=fname,
                size_mb=None,
                content_type=None,
                requires_auth=False,
                error="URL not found (404). Check the download link is correct.",
            )
        return PreflightResult(
            ok=False,
            filename=fname,
            size_mb=None,
            content_type=None,
            requires_auth=False,
            error=safe_detail(e, "Could not fetch model metadata over HTTP.", log=log),
        )
    except Exception as e:
        return PreflightResult(
            ok=False,
            filename=fname,
            size_mb=None,
            content_type=None,
            requires_auth=False,
            error=safe_detail(e, "Preflight check failed.", log=log),
        )


# ---------------------------------------------------------------------------
# HuggingFace model search
# ---------------------------------------------------------------------------


@router.get("/hf/search")
def search_huggingface_models(q: str, limit: int = 10) -> list[dict[str, Any]]:
    """Search HuggingFace for GGUF models matching a query."""
    import urllib.request as _req
    import json as _json

    url = (
        f"https://huggingface.co/api/models"
        f"?search={q}&filter=gguf&sort=downloads&direction=-1&limit={limit}"
    )
    try:
        with _req.urlopen(url, timeout=10) as resp:  # noqa: S310  # nosec B310  # URL is a hardcoded https:// constant
            models = _json.loads(resp.read())
        return [
            {
                "id": m.get("modelId", m.get("id", "")),
                "downloads": m.get("downloads", 0),
                "likes": m.get("likes", 0),
                "tags": m.get("tags", [])[:5],
            }
            for m in models
        ]
    except Exception as e:
        raise HTTPException(
            status_code=503, detail=safe_detail(e, "HuggingFace search failed.", log=log)
        ) from e


# ---------------------------------------------------------------------------
# Fix history (error → fix → outcome learning)
# ---------------------------------------------------------------------------


@router.post("/fix-history")
def record_fix(rec: FixRecord) -> dict[str, Any]:
    """Store an LLM-suggested fix and its outcome.

    ``outcome`` is constrained to the canonical ``fix_history.outcome`` vocabulary
    (``FIX_HISTORY_OUTCOMES``, #1212) so this direct-INSERT path cannot write a label
    the learning layer can't interpret — the same fail-closed posture as the
    ``PUT /fix-history/{id}/outcome`` route below.
    """
    from backend.agent.fix_outcome import FIX_HISTORY_OUTCOMES

    if rec.outcome not in FIX_HISTORY_OUTCOMES:
        raise HTTPException(
            status_code=422,
            detail=f"outcome must be one of {sorted(FIX_HISTORY_OUTCOMES)}",
        )
    with StateDB() as db:
        db.execute(
            """INSERT OR REPLACE INTO fix_history
               (app_key, error_type, context, suggested_fix, outcome, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                rec.app_key,
                rec.error_type,
                rec.context[:500],
                rec.suggested_fix[:500],
                rec.outcome,
                int(time.time()),
            ),
        )
    return {"ok": True}


@router.put("/fix-history/{fix_id}/outcome")
def update_fix_outcome(fix_id: int, outcome: str) -> dict[str, Any]:
    """Mark a fix as successful or failed — drives the feedback loop."""
    if outcome not in ("success", "failure"):
        raise HTTPException(status_code=422, detail="outcome must be 'success' or 'failure'")
    with StateDB() as db:
        db.execute(
            "UPDATE fix_history SET outcome=? WHERE id=?",
            (outcome, fix_id),
        )
    return {"ok": True}


@router.get("/fix-history")
def get_fix_history(app_key: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
    """Return fix history, optionally filtered by app."""
    with StateDB() as db:
        if app_key:
            rows = db.execute(
                "SELECT * FROM fix_history WHERE app_key=? ORDER BY created_at DESC LIMIT ?",
                (app_key, limit),
            ).fetchall()
        else:
            rows = db.execute(
                "SELECT * FROM fix_history ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# LLM Model Registry — active models + task routing
# ---------------------------------------------------------------------------


@router.get("/registry")
def get_registry() -> dict[str, Any]:
    """Return all model registry entries, routing table, and file sync status."""
    import urllib.request as _req2
    import json as _j2
    from backend.core.llm_router import (
        get_all_models,
        routing_table,
        sync_registry_with_files,
    )

    # Collect all known model names: local GGUFs + Ollama models
    local_files = list_gguf_files(_models_dir())
    all_names = [f["filename"] for f in local_files]

    # Add Ollama-managed models
    try:
        from backend.core.state import StateDB as _SDB2

        with _SDB2() as _db2:
            _cfg_raw = _db2.get_setting("llm_agent_config")
        _cfg2 = _j2.loads(_cfg_raw) if _cfg_raw else {}
        _url = _cfg2.get("ollama_url", "http://localhost:11434")
        if not _url.startswith(("http://", "https://")):
            raise ValueError(f"Unsupported Ollama URL scheme: {_url}")
        _req_obj = _req2.Request(  # noqa: S310  # scheme validated above
            f"{_url}/api/tags",
            headers={"User-Agent": "SLOP/3.0"},
        )
        with _req2.urlopen(_req_obj, timeout=3) as _resp:  # noqa: S310  # nosec B310  # scheme validated above
            _data = _j2.loads(_resp.read())
        for _m in _data.get("models", []):
            _name = _m.get("name", "")
            if _name and _name not in all_names:
                all_names.append(_name)
    except Exception:  # noqa: S110  # Ollama unreachable — omit managed models from list
        pass

    # Sync all names into registry (non-destructive)
    sync_registry_with_files(all_names)

    models_out = []
    for m in get_all_models():
        # Only include models that still exist on disk
        on_disk = any(f["filename"] == m.filename for f in local_files)
        models_out.append(
            {
                "filename": m.filename,
                "display_name": m.display_name,
                "enabled": m.enabled,
                "capabilities": m.capabilities,
                "task_scores": m.task_scores,
                "priority": m.priority,
                "context_window": m.context_window,
                "ollama_name": m.ollama_name,
                "notes": m.notes,
                "on_disk": on_disk,
            }
        )

    return {
        "models": models_out,
        "routing_table": routing_table(),
        "enabled_count": sum(1 for m in models_out if m["enabled"]),
    }


@router.put("/registry/{filename:path}")
def update_registry_entry(filename: str, req: RegistryUpdateRequest) -> dict[str, Any]:
    """Update a model's registry entry (enable/disable, capabilities, etc.)."""
    from backend.core.llm_router import upsert_model, routing_table

    upsert_model(
        filename,
        enabled=req.enabled,
        display_name=req.display_name,
        capabilities=req.capabilities,
        task_scores=req.task_scores,
        priority=req.priority,
        context_window=req.context_window,
        ollama_name=req.ollama_name,
        notes=req.notes,
    )
    return {"ok": True, "routing_table": routing_table()}


@router.get("/routing")
def get_routing_table() -> list[dict[str, Any]]:
    """Return current task → model routing table."""
    from backend.core.llm_router import routing_table

    return routing_table()


@router.get("/routing-log")
def get_routing_log(limit: int = 50) -> list[dict[str, Any]]:
    """Return recent LLM routing decisions."""
    from backend.core.state import StateDB

    with StateDB() as db:
        # Table created by migration 015_llm_routing_log.sql — no ad-hoc DDL here.
        rows = db.execute(
            """SELECT * FROM llm_routing_log
               ORDER BY ts DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]
