"""backend/api/models_schemas.py

Pydantic request/response DTOs for the models (LLM) API router.

Extracted from ``models.py`` (#1302 linecount drain) — these are leaf
request/response schemas with no dependency on the router's handlers; the
router re-imports them so ``response_model=`` references and handler bodies
resolve unchanged. ``HardwareEvalResult`` references ``HardwareEvalStep``
(defined above it here, so the forward ref resolves within this module).
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class GGUFFileInfo(BaseModel):
    filename: str
    path: str
    size_mb: float
    valid: bool
    gguf_version: int | None
    error: str | None
    warning: str | None


class ValidateRequest(BaseModel):
    path: str = Field(..., description="Absolute path to the GGUF file on the server")


class ValidateResponse(BaseModel):
    valid: bool
    size_mb: float
    gguf_version: int | None
    error: str | None
    warning: str | None


class DownloadRequest(BaseModel):
    url: str = Field(
        ...,
        description=(
            "HuggingFace shorthand (hf://org/repo/file.gguf), "
            "HuggingFace URL, or any direct HTTPS download URL"
        ),
    )
    filename: str | None = Field(
        None,
        description="Override the destination filename. Defaults to the URL filename.",
    )


class AgentConfig(BaseModel):
    backend: str = Field(
        "ollama",
        description="Inference backend: 'ollama' or 'llamacpp'",
    )
    # Ollama settings
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "phi4-mini"
    # llama.cpp settings
    llamacpp_url: str = "http://localhost:8081"
    llamacpp_model_file: str = ""  # filename inside models_dir
    # Agent behaviour
    confidence_threshold: float = 0.85
    auto_restart: bool = True
    notify_on_auto_fix: bool = True
    notify_on_escalation: bool = True
    ntfy_topic: str = "slop"


class EvaluateResult(BaseModel):
    passed: bool
    backend: str
    model: str
    inference_seconds: float
    parsed_correctly: bool
    identified_error: bool
    response_preview: str
    score: str  # pass | warn | fail
    reason: str | None


class HardwareEvalStep(BaseModel):
    label: str
    status: str  # ok | warn | error | info
    detail: str


class HardwareEvalResult(BaseModel):
    steps: list[HardwareEvalStep]
    verdict: str  # can_run | runs_slowly | cannot_run
    summary: str
    recommended_quantization: str
    estimated_tokens_per_second: int
    inference_mode: str  # cpu | gpu


class PreflightResult(BaseModel):
    ok: bool
    filename: str
    size_mb: float | None
    content_type: str | None
    requires_auth: bool
    error: str | None


class FixRecord(BaseModel):
    app_key: str
    error_type: str
    context: str
    suggested_fix: str
    # SSOT: backend.agent.fix_outcome.FIX_HISTORY_OUTCOMES
    # (pending | success | failure | failed_verification | user_approved_manual)
    outcome: str = "pending"


class ModelRegistryEntry(BaseModel):
    filename: str
    display_name: str
    enabled: bool
    capabilities: list[str]
    task_scores: dict[str, float]
    priority: int
    context_window: int
    ollama_name: str | None
    notes: str


class RegistryUpdateRequest(BaseModel):
    enabled: bool | None = None
    display_name: str | None = None
    capabilities: list[str] | None = None
    task_scores: dict[str, float] | None = None
    priority: int | None = None
    context_window: int | None = None
    ollama_name: str | None = None
    notes: str | None = None
