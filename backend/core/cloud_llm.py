"""backend/core/cloud_llm.py

Cloud LLM escalation — multi-provider cascade with sanitization and cost tracking.

Architecture:
  1. Sanitize context (replace secrets/IPs with placeholders)
  2. Estimate token count and cost before sending
  3. Check monthly cost limit — block if exceeded
  4. Send to configured provider (respects cascade tier)
  5. Reverse-sanitize response (restore real values)
  6. Record usage in cloud_llm_usage table
  7. Index response into local RAG knowledge base

Privacy:
  - All sensitive values replaced with ••PLACEHOLDER•• before sending
  - User can preview sanitized payload before confirming
  - Substitution map never leaves the local system
  - User can mark specific keys as "always redact" in settings

Cost:
  - Token count estimated locally (no API call needed)
  - Monthly spend tracked in cloud_llm_usage
  - Configurable per-provider and global monthly limits
  - Hard block when limit reached
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any

from backend.core.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Provider registry
# ---------------------------------------------------------------------------

PROVIDERS: dict[str, dict[str, Any]] = {
    "groq": {
        "label": "Groq Cloud",
        "base_url": "https://api.groq.com/openai/v1",
        "env_key": "GROQ_API_KEY",
        "default_model": "llama-3.3-70b-versatile",
        "free_tier": True,
        "free_rpm": 30,
        "free_daily_tokens": 14400,
        "input_cost_per_1m": 0.59,
        "output_cost_per_1m": 0.79,
        "privacy": "us",
        "notes": "Fastest inference. No card needed. 30 RPM free.",
    },
    "cerebras": {
        "label": "Cerebras",
        "base_url": "https://api.cerebras.ai/v1",
        "env_key": "CEREBRAS_API_KEY",
        "default_model": "llama-3.3-70b",
        "free_tier": True,
        "free_rpm": 30,
        "free_daily_tokens": 1_000_000,
        "input_cost_per_1m": 0.10,
        "output_cost_per_1m": 0.10,
        "privacy": "us",
        "notes": "Most generous free tier: 1M tokens/day. Very fast.",
    },
    "google": {
        "label": "Google AI Studio",
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
        "env_key": "GOOGLE_AI_API_KEY",
        "default_model": "gemini-2.0-flash",
        "free_tier": True,
        "free_rpm": 10,
        "free_daily_tokens": 250,  # requests not tokens
        "input_cost_per_1m": 0.30,
        "output_cost_per_1m": 1.00,
        "privacy": "us",
        "notes": "Best free quality. 250 req/day Gemini Flash free. 1M context.",
    },
    "mistral": {
        "label": "Mistral AI",
        "base_url": "https://api.mistral.ai/v1",
        "env_key": "MISTRAL_API_KEY",
        "default_model": "mistral-small-latest",
        "free_tier": True,
        "free_rpm": 2,
        "free_monthly_tokens": 1_000_000_000,
        "input_cost_per_1m": 0.02,
        "output_cost_per_1m": 0.06,
        "privacy": "eu",
        "notes": "1B tokens/month free. EU data residency. Cheapest paid rate.",
    },
    "openrouter": {
        "label": "OpenRouter",
        "base_url": "https://openrouter.ai/api/v1",
        "env_key": "OPENROUTER_API_KEY",
        "default_model": "meta-llama/llama-3.3-70b-instruct:free",
        "free_tier": True,
        "free_rpm": 20,
        "free_daily_requests": 50,
        "input_cost_per_1m": 0.0,  # free models are $0
        "output_cost_per_1m": 0.0,
        "privacy": "us",
        "notes": "30+ free models via one key. 300+ total models.",
    },
    "cohere": {
        "label": "Cohere",
        "base_url": "https://api.cohere.ai/compatibility/v1",
        "env_key": "COHERE_API_KEY",
        "default_model": "command-r7b-12-2024",
        "free_tier": True,
        "free_rpm": 5,
        "free_monthly_tokens": 100_000,
        "input_cost_per_1m": 0.038,
        "output_cost_per_1m": 0.15,
        "privacy": "us",
        "notes": "Trial key: 100k tokens/month. Excellent at structured JSON output.",
    },
    "z_ai": {
        "label": "z.ai (Zhipu GLM)",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "env_key": "ZAI_API_KEY",
        "default_model": "glm-4.7-flash",
        "free_tier": True,
        "free_rpm": 30,
        "free_models": ["glm-4.7-flash", "glm-4.5-flash"],
        "input_cost_per_1m": 0.06,
        "output_cost_per_1m": 0.06,
        "privacy": "cn",
        "notes": "GLM-4.7-Flash and GLM-4.5-Flash permanently free.",
    },
    "featherless": {
        "label": "Featherless.ai",
        "base_url": "https://api.featherless.ai/v1",
        "env_key": "FEATHERLESS_API_KEY",
        "default_model": "meta-llama/Llama-3.3-70B-Instruct",
        "free_tier": False,
        "subscription_usd": 10.0,
        "input_cost_per_1m": 0.0,  # flat-rate subscription
        "output_cost_per_1m": 0.0,
        "privacy": "us",
        "notes": "$10/month unlimited tokens. 20,000+ open-source models. No logging.",
    },
    "anthropic": {
        "label": "Anthropic (Claude)",
        "base_url": "https://api.anthropic.com/v1",
        "env_key": "ANTHROPIC_API_KEY",
        "default_model": "claude-haiku-4-5",
        "free_tier": False,
        "input_cost_per_1m": 0.80,
        "output_cost_per_1m": 4.00,
        "privacy": "us",
        "notes": "Best reasoning. Premium quality escalation only.",
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "env_key": "OPENAI_API_KEY",
        "default_model": "gpt-4o-mini",
        "free_tier": False,
        "input_cost_per_1m": 0.15,
        "output_cost_per_1m": 0.60,
        "privacy": "us",
        "notes": "Wide model selection. gpt-4o-mini is cost-effective mid-tier.",
    },
    "siliconflow": {
        "label": "SiliconFlow",
        "base_url": "https://api.siliconflow.cn/v1",
        "env_key": "SILICONFLOW_API_KEY",
        "default_model": "deepseek-ai/DeepSeek-V3",
        "free_tier": True,
        "free_credits_usd": 1.0,
        "input_cost_per_1m": 0.14,
        "output_cost_per_1m": 0.28,
        "privacy": "cn",
        "notes": "$1 free credit. DeepSeek/Qwen models. Very cheap. China infrastructure.",
    },
    "awan": {
        "label": "Awan LLM",
        "base_url": "https://api.awanllm.com/v1",
        "env_key": "AWAN_API_KEY",
        "default_model": "Meta-Llama-3.1-8B-Instruct",
        "free_tier": True,
        "free_rpm": 60,
        "free_daily_tokens": 100_000,
        "input_cost_per_1m": 0.18,
        "output_cost_per_1m": 0.18,
        "privacy": "us",
        "notes": "OpenAI-compatible. Free tier available at awanllm.com. No card needed.",
    },
}

# Default cascade: fast-free → quality-free → quality-paid
DEFAULT_CASCADE = ["groq", "cerebras", "google", "mistral", "anthropic"]


# ---------------------------------------------------------------------------
# Sanitization
# ---------------------------------------------------------------------------

# Patterns that look like secrets — redact before sending
_SECRET_PATTERNS = [
    re.compile(
        r'(cf[a-z_]*token|api[_-]?key|password|secret|token|bearer)\s*[=:]\s*([^\s\n\'"]{8,})',
        re.IGNORECASE,
    ),
    re.compile(r"\b[A-Za-z0-9_-]{32,}\b"),  # long random strings (API keys, tokens)
    re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"),  # IPv4 addresses
    re.compile(r"/home/[^/\s]+"),  # home paths with usernames
]


def sanitize_context(text: str) -> tuple[str, dict[str, str]]:
    """Replace sensitive values with placeholders.

    Returns: (sanitized_text, substitution_map)
    The substitution map is used to restore real values in the response.
    """
    subst: dict[str, str] = {}
    counter = [0]

    def _replace(match_text: str, category: str) -> str:
        # Check if already mapped
        for placeholder, original in subst.items():
            if original == match_text:
                return placeholder
        counter[0] += 1
        placeholder = f"••{category.upper()}_{counter[0]}••"
        subst[placeholder] = match_text
        return placeholder

    result = text

    # Read .env for known secret values first
    try:
        from backend.core.config import config as _cfg

        if _cfg.env_file.exists():
            for line in _cfg.env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                val = val.strip()
                if len(val) >= 8 and val in result:
                    result = result.replace(val, _replace(val, key.strip()[:12]))
    except Exception:  # noqa: S110  # best-effort .env redaction; skip if file unreadable
        pass

    # Pattern-based redaction
    for pattern in _SECRET_PATTERNS:
        for match in pattern.finditer(result):
            full = match.group(0)
            if len(full) >= 8 and full in result:
                result = result.replace(full, _replace(full, "VALUE"))

    return result, subst


def restore_context(text: str, subst: dict[str, str]) -> str:
    """Reverse sanitization — restore real values from placeholder map."""
    result = text
    for placeholder, original in subst.items():
        result = result.replace(placeholder, original)
    return result


# ---------------------------------------------------------------------------
# Token estimation (local, no API call)
# ---------------------------------------------------------------------------


def estimate_tokens(text: str) -> int:
    """Rough token count: ~4 chars per token. Accurate to ±20%."""
    return max(1, len(text) // 4)


def estimate_cost(provider_key: str, prompt_tokens: int, completion_tokens: int) -> float:
    """Estimate cost in USD for a call."""
    p = PROVIDERS.get(provider_key, {})
    input_cost = p.get("input_cost_per_1m", 0) * prompt_tokens / 1_000_000
    output_cost = p.get("output_cost_per_1m", 0) * completion_tokens / 1_000_000
    return float(round(input_cost + output_cost, 6))


# ---------------------------------------------------------------------------
# Cost limit enforcement
# ---------------------------------------------------------------------------


def _get_monthly_spend(provider_key: str | None = None) -> float:
    """Return total spend this calendar month."""
    try:
        from backend.core.state import StateDB
        import datetime

        first_of_month = int(
            datetime.datetime(
                datetime.date.today().year, datetime.date.today().month, 1
            ).timestamp()
        )
        with StateDB() as db:
            if provider_key:
                row = db.execute(
                    "SELECT SUM(cost_usd) FROM cloud_llm_usage WHERE provider=? AND created_at>=?",
                    (provider_key, first_of_month),
                ).fetchone()
            else:
                row = db.execute(
                    "SELECT SUM(cost_usd) FROM cloud_llm_usage WHERE created_at>=?",
                    (first_of_month,),
                ).fetchone()
            return float(row[0] or 0.0)
    except Exception:
        return 0.0


def _check_cost_limit(provider_key: str, estimated_cost: float) -> tuple[bool, str]:
    """Return (ok, reason). Blocks if limit would be exceeded."""
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            global_limit = float(db.get_setting("cloud_llm_monthly_limit_usd") or "1.00")
            provider_limit_str = db.get_setting(f"cloud_llm_limit_{provider_key}")
            provider_limit = float(provider_limit_str) if provider_limit_str else None
    except Exception:
        global_limit = 1.00
        provider_limit = None

    global_spend = _get_monthly_spend()
    if global_spend + estimated_cost > global_limit:
        return False, (
            f"Monthly limit of ${global_limit:.2f} would be exceeded "
            f"(current: ${global_spend:.4f}, estimated: ${estimated_cost:.4f}). "
            f"Raise the limit in Settings → AI → Cost."
        )

    if provider_limit is not None:
        provider_spend = _get_monthly_spend(provider_key)
        if provider_spend + estimated_cost > provider_limit:
            return False, (
                f"{PROVIDERS[provider_key]['label']} monthly limit of ${provider_limit:.2f} "
                f"would be exceeded. Raise in Settings → AI → Cost."
            )

    return True, ""


def _record_usage(
    provider_key: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    cost: float,
    purpose: str = "",
    app_key: str = "",
) -> None:
    """Record API call in cloud_llm_usage table."""
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            db.execute(
                """INSERT INTO cloud_llm_usage
                   (provider, model, prompt_tokens, completion_tokens, total_tokens,
                    cost_usd, sanitized, purpose, app_key, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (
                    provider_key,
                    model,
                    prompt_tokens,
                    completion_tokens,
                    prompt_tokens + completion_tokens,
                    cost,
                    purpose,
                    app_key,
                    int(time.time()),
                ),
            )
    except Exception as e:
        log.debug("Failed to record cloud LLM usage: %s", e)


# ---------------------------------------------------------------------------
# OpenAI-compatible call (works for Groq, Cerebras, Mistral, etc.)
# ---------------------------------------------------------------------------


async def _call_openai_compatible(
    base_url: str, api_key: str, model: str, prompt: str, timeout: int = 30
) -> tuple[str, int, int]:
    """Call any OpenAI-compatible endpoint. Returns (response_text, in_tokens, out_tokens)."""
    from backend.core.url_guard_httpx import pinned_async_client

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 500,
        "temperature": 0.1,
    }
    async with pinned_async_client(timeout=timeout) as client:
        resp = await client.post(f"{base_url}/chat/completions", json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()

    choice = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return choice, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


# ---------------------------------------------------------------------------
# Main escalation entry point
# ---------------------------------------------------------------------------


@dataclass
class EscalationResult:
    ok: bool
    response: str = ""
    provider_used: str = ""
    model_used: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    estimated_cost: float = 0.0
    sanitized: bool = True
    error: str = ""
    blocked_reason: str = ""


def _resolve_cascade(cascade: list[str] | None) -> list[str]:
    """Determine the provider cascade — explicit arg wins; otherwise
    pull from settings DB; fall back to DEFAULT_CASCADE on any error."""
    if cascade is not None:
        return cascade
    try:
        from backend.core.state import StateDB

        with StateDB() as db:
            cascade_setting = db.get_setting("cloud_llm_cascade")
            return cascade_setting.split(",") if cascade_setting else DEFAULT_CASCADE
    except Exception:
        return DEFAULT_CASCADE


def _read_api_key(env_key: str) -> str:
    """Pull `env_key=value` out of `.env`. Returns empty string when
    the file is missing, the key is absent, or any read error occurs."""
    from backend.core.config import config as _cfg

    try:
        if not _cfg.env_file.exists():
            return ""
        for line in _cfg.env_file.read_text().splitlines():
            if line.strip().startswith(f"{env_key}="):
                return line.strip().split("=", 1)[1].strip()
    except Exception:  # noqa: S110  # best-effort .env read; return empty if unavailable
        pass
    return ""


def _index_escalation_in_rag(prompt: str, response: str, purpose: str) -> None:
    """Append the prompt/response to the RAG knowledge base + force
    rebuild on next query. Best-effort; failures are silenced."""
    try:
        import backend.core.rag as _rag_mod

        _rag_mod.KNOWLEDGE_BASE.append(
            {
                "id": f"escalation_{int(time.time())}",
                "title": f"Cloud LLM escalation: {purpose}",
                "text": f"Problem context: {prompt[:500]}\nSolution: {response[:500]}",
            }
        )
        _rag_mod._built_at = 0  # force RAG rebuild on next query
    except Exception:  # noqa: S110  # best-effort RAG index update; failures are silenced by design
        pass


async def _try_one_provider(
    provider_key: str,
    sanitized_prompt: str,
    subst: dict[str, str],
    raw_prompt: str,
    purpose: str,
    app_key: str,
    user_confirmed: bool,
    est_prompt_tokens: int,
    est_completion_tokens: int,
) -> EscalationResult | None:
    """Attempt one provider. Returns:
    - EscalationResult on success (or on a `requires-confirmation`
      sentinel response that the caller surfaces unchanged)
    - None when the caller should `continue` to the next provider
      (key missing, cost limit hit, transport error)
    """
    provider = PROVIDERS.get(provider_key)
    if not provider:
        return None

    api_key = _read_api_key(provider["env_key"])
    if not api_key:
        return None  # skip paid (no key) and free (no key) alike

    est_cost = estimate_cost(provider_key, est_prompt_tokens, est_completion_tokens)
    ok, reason = _check_cost_limit(provider_key, est_cost)
    if not ok:
        log.info("Skipping %s: %s", provider_key, reason)
        return None

    if not provider.get("free_tier") and not user_confirmed:
        return EscalationResult(
            ok=False,
            error=f"Provider '{provider['label']}' requires user confirmation.",
            estimated_cost=est_cost,
            provider_used=provider_key,
        )

    model = provider.get("default_model", "")
    try:
        response_raw, in_tok, out_tok = await _call_openai_compatible(
            base_url=provider["base_url"],
            api_key=api_key,
            model=model,
            prompt=sanitized_prompt,
            timeout=30,
        )
    except Exception as e:
        log.warning("Provider %s failed: %s — trying next", provider_key, e)
        return None

    response = restore_context(response_raw, subst)
    actual_cost = estimate_cost(
        provider_key,
        in_tok or est_prompt_tokens,
        out_tok or est_completion_tokens,
    )
    _record_usage(
        provider_key,
        model,
        in_tok or est_prompt_tokens,
        out_tok or est_completion_tokens,
        actual_cost,
        purpose,
        app_key,
    )
    _index_escalation_in_rag(raw_prompt, response, purpose)
    log.info(
        "Cloud escalation succeeded via %s (%d tokens, $%.4f)",
        provider_key,
        (in_tok or 0) + (out_tok or 0),
        actual_cost,
    )
    return EscalationResult(
        ok=True,
        response=response,
        provider_used=provider_key,
        model_used=model,
        prompt_tokens=in_tok or est_prompt_tokens,
        completion_tokens=out_tok or est_completion_tokens,
        cost_usd=actual_cost,
        estimated_cost=est_cost,
        sanitized=bool(subst),
    )


async def escalate_to_cloud(
    prompt: str,
    app_key: str = "",
    purpose: str = "health_escalation",
    user_confirmed: bool = False,
    cascade: list[str] | None = None,
) -> EscalationResult:
    """Send a prompt to the cloud LLM cascade.

    Safety:
    - Sanitizes prompt before sending (secrets → placeholders)
    - Checks cost limits before sending
    - Records usage after sending
    - Indexes response into RAG knowledge base
    - user_confirmed=True required for non-free-tier providers

    Returns EscalationResult with response, usage, and cost data.

    Step 2.7 phase-3 closure: cascade resolution, .env parsing, RAG
    indexing, and the per-provider attempt all extracted into helpers
    (`_resolve_cascade`, `_read_api_key`, `_index_escalation_in_rag`,
    `_try_one_provider`) — drops complexity from 15 to ≤ 4.
    """
    cascade = _resolve_cascade(cascade)
    sanitized_prompt, subst = sanitize_context(prompt)
    est_prompt_tokens = estimate_tokens(sanitized_prompt)
    est_completion_tokens = 300  # assume ~300 tokens response

    for provider_key in cascade:
        result = await _try_one_provider(
            provider_key,
            sanitized_prompt,
            subst,
            prompt,
            purpose,
            app_key,
            user_confirmed,
            est_prompt_tokens,
            est_completion_tokens,
        )
        if result is not None:
            return result

    return EscalationResult(
        ok=False,
        error="All providers in cascade failed or are unavailable.",
    )
