"""backend/health/checker_llm_dispatch.py

LLM call transport/dispatch layer for the health agent.

Extracted from checker_llm.py (Core Rule 8.1 — file-size discipline). These are
the pure HTTP transport functions: provider-routing + per-provider call shapes
(ollama / cloud / openai-compatible) + router-dispatched cloud spend metering.
They hold no module state (`_llm_state` and the diagnosis orchestration stay in
checker_llm.py) — the only shared concern is ADR-0021 scrub-before-cloud-egress,
which lives in `_dispatch_llm_call` here.

ADR-0021 gate note: the `check_llm_outbound_scrubbed` enforcement gate AST-walks
`backend/health/*.py` for any module defining `_dispatch_llm_call` and verifies it
imports + calls `scrub()`. Moving the function here keeps it covered — do NOT drop
the `scrub()` call or the `from backend.agent.scrub import scrub` import.

All symbols are re-exported from checker_llm.py (and thence checker.py) for
backward compatibility.
"""

from __future__ import annotations

from typing import Any

import httpx

from backend.agent.scrub import scrub
from backend.agent.scrub import is_external
from backend.core.logging import get_logger

log = get_logger(__name__)


async def _call_ollama(client: httpx.AsyncClient, prompt: str, ollama_url: str, model: str) -> str:
    """Hit ollama /api/generate; return the raw response string."""
    resp = await client.post(
        f"{ollama_url}/api/generate",
        json={"model": model, "prompt": prompt, "stream": False, "format": "json"},
    )
    resp.raise_for_status()
    return resp.json().get("response", "") or ""


async def _call_cloud_provider(
    client: httpx.AsyncClient, prompt: str, provider: str, api_key: str, model: str
) -> str:
    """Hit an OpenAI-style cloud /v1/chat/completions; return the assistant content."""
    from typing import Any as _Any
    from backend.core.cloud_llm import PROVIDERS as _CP

    _p_cfg = _CP.get(provider, {})
    _base = _p_cfg.get("base_url", "").rstrip("/")
    if not _base:
        raise ValueError(f"Unknown provider '{provider}'")
    _endpoint = f"{_base}/chat/completions"
    hdrs = {
        "Authorization": f"Bearer {api_key}",
        "HTTP-Referer": "https://github.com/SLOP-Platform/SLOP",
        "X-Title": "SLOP Health Agent",
    }
    cloud_model = model or _p_cfg.get("default_model", "")
    _rf: dict[str, _Any] = {}
    if provider not in ("anthropic",):
        _rf = {"response_format": {"type": "json_object"}}
    if provider == "anthropic":
        hdrs["anthropic-version"] = "2023-06-01"
    resp = await client.post(
        _endpoint,
        headers=hdrs,
        json={"model": cloud_model, "messages": [{"role": "user", "content": prompt}], **_rf},
    )
    resp.raise_for_status()
    data = resp.json()
    # #1115: record router-dispatched cloud spend so the 24h budget cap is actually
    # enforced here too. The escalate_to_cloud path records via its own helper; this
    # is the OTHER cloud-call site and was previously unrecorded → toothless cap.
    _record_cloud_dispatch_usage(provider, cloud_model, data.get("usage", {}))
    return data.get("choices", [{}])[0].get("message", {}).get("content", "") or ""


def _record_cloud_dispatch_usage(provider: str, model: str, usage: dict[str, Any]) -> None:
    """Record a router-dispatched cloud call in cloud_llm_usage (#1115).

    Best-effort and side-effect-only: it must NEVER raise into the LLM call path.
    Mirrors the escalate path's estimate_cost + _record_usage so router-dispatched
    spend counts against the 24h budget cap (`_check_cost_limit` SUMs cost_usd by
    provider + time window). purpose='router-dispatch' distinguishes these rows
    from 'health_escalation'; per-app attribution (app_key) is a future enhancement
    (the cap only needs provider + cost + time)."""
    try:
        from backend.core.cloud_llm import _record_usage, estimate_cost

        in_tok = int(usage.get("prompt_tokens", 0) or 0)
        out_tok = int(usage.get("completion_tokens", 0) or 0)
        cost = estimate_cost(provider, in_tok, out_tok)
        _record_usage(provider, model, in_tok, out_tok, cost, "router-dispatch", "")
    except Exception as e:  # never break the call path on a metering hiccup
        log.debug("Failed to record router cloud usage (#1115): %s", e)


async def _call_openai_compatible(
    client: httpx.AsyncClient, prompt: str, ollama_url: str, api_key: str, model: str
) -> str:
    """Hit an OpenAI-shaped local server (llamacpp / shimmy / localai)."""
    hdrs = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    resp = await client.post(
        f"{ollama_url}/v1/chat/completions",
        headers=hdrs,
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        },
    )
    resp.raise_for_status()
    return resp.json().get("choices", [{}])[0].get("message", {}).get("content", "") or ""


async def _dispatch_llm_call(
    client: httpx.AsyncClient,
    prompt: str,
    ollama_url: str,
    provider: str,
    api_key: str,
    model: str,
    cloud_providers: set[str],
    *,
    allow_raw: bool = False,
) -> str:
    """Dispatch the LLM call to ollama / cloud / openai-compatible by provider.

    allow_raw: when True, skip scrub() even for cloud providers (opt-out).
    Default False means cloud-bound prompts are always scrubbed (ADR-0021).
    """
    if (is_external(provider) or provider in cloud_providers) and not allow_raw:
        prompt = scrub(prompt)
    if provider == "ollama":
        return await _call_ollama(client, prompt, ollama_url, model)
    if provider in cloud_providers:
        return await _call_cloud_provider(client, prompt, provider, api_key, model)
    return await _call_openai_compatible(client, prompt, ollama_url, api_key, model)
