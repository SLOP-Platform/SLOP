"""backend.agent.router.registry — Explicit provider registry (ADR-0010 compliant).

PROVIDER_REGISTRY is a plain dict literal.  Names are derived by IMPORTING the
canonical frozensets from backend.core.agent (_CLOUD_PROVIDERS,
_LOCAL_OAI_PROVIDERS) so that any addition to those sets automatically shows up
here as an entry in the registry.

No dynamic file scanning, no __import__, no importlib — see ADR-0010.
"""

from __future__ import annotations

from typing import Any

import structlog

from backend.core.agent import _CLOUD_PROVIDERS, _LOCAL_OAI_PROVIDERS
from backend.agent.router.types import ProviderSpec, Tier

log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Static per-provider metadata
# ---------------------------------------------------------------------------
# Tiers and cost figures are conservative defaults.  All local providers have
# cost_per_1k=0.0.  Cloud providers have ballpark figures; free-tier providers
# are marked with a very low cost so the selector prefers them over paid ones.

_PROVIDER_META: dict[str, dict[str, Any]] = {
    # ── Local providers ────────────────────────────────────────────────────
    "ollama": {
        "kind": "ollama",
        "tiers": frozenset({Tier.SIMPLE, Tier.STANDARD, Tier.COMPLEX}),
        "cost_per_1k": 0.0,
        "local": True,
    },
    "llamacpp": {
        "kind": "llamacpp",
        "tiers": frozenset({Tier.SIMPLE, Tier.STANDARD, Tier.COMPLEX}),
        "cost_per_1k": 0.0,
        "local": True,
    },
    # shimmy and localai — from _LOCAL_OAI_PROVIDERS
    "shimmy": {
        "kind": "openai-compat",
        "tiers": frozenset({Tier.SIMPLE, Tier.STANDARD}),
        "cost_per_1k": 0.0,
        "local": True,
    },
    "localai": {
        "kind": "openai-compat",
        "tiers": frozenset({Tier.SIMPLE, Tier.STANDARD}),
        "cost_per_1k": 0.0,
        "local": True,
    },
    # ── Cloud providers ────────────────────────────────────────────────────
    # groq — fast, free tier, good for SIMPLE/STANDARD
    "groq": {
        "kind": "openai-compat",
        "tiers": frozenset({Tier.SIMPLE, Tier.STANDARD, Tier.COMPLEX}),
        "cost_per_1k": 0.00079,  # $0.79/1M output tokens
        "local": False,
    },
    # cerebras — very fast, free 1M tokens/day
    "cerebras": {
        "kind": "openai-compat",
        "tiers": frozenset({Tier.SIMPLE, Tier.STANDARD, Tier.COMPLEX}),
        "cost_per_1k": 0.0001,  # $0.10/1M
        "local": False,
    },
    # openrouter — free models available; gateway to many providers
    "openrouter": {
        "kind": "openai-compat",
        "tiers": frozenset({Tier.SIMPLE, Tier.STANDARD, Tier.COMPLEX, Tier.REASONING}),
        "cost_per_1k": 0.0,  # free-tier models default
        "local": False,
    },
    # mistral — 1B tokens/month free; EU data residency
    "mistral": {
        "kind": "openai-compat",
        "tiers": frozenset({Tier.SIMPLE, Tier.STANDARD, Tier.COMPLEX}),
        "cost_per_1k": 0.00006,  # $0.06/1M output
        "local": False,
    },
    # cohere — good structured output, free trial
    "cohere": {
        "kind": "openai-compat",
        "tiers": frozenset({Tier.SIMPLE, Tier.STANDARD}),
        "cost_per_1k": 0.00015,  # $0.15/1M output
        "local": False,
    },
    # google — Gemini Flash free tier, 1M context
    "google": {
        "kind": "openai-compat",
        "tiers": frozenset({Tier.SIMPLE, Tier.STANDARD, Tier.COMPLEX, Tier.REASONING}),
        "cost_per_1k": 0.001,  # $1.00/1M output
        "local": False,
    },
    # anthropic — best reasoning; premium escalation only
    "anthropic": {
        "kind": "anthropic",
        "tiers": frozenset({Tier.COMPLEX, Tier.REASONING}),
        "cost_per_1k": 0.004,  # $4.00/1M output (Claude Haiku)
        "local": False,
    },
    # openai — wide model selection
    "openai": {
        "kind": "openai-compat",
        "tiers": frozenset({Tier.SIMPLE, Tier.STANDARD, Tier.COMPLEX, Tier.REASONING}),
        "cost_per_1k": 0.0006,  # $0.60/1M output (gpt-4o-mini)
        "local": False,
    },
    # nim — NVIDIA Inference Microservices
    "nim": {
        "kind": "openai-compat",
        "tiers": frozenset({Tier.SIMPLE, Tier.STANDARD, Tier.COMPLEX}),
        "cost_per_1k": 0.001,
        "local": False,
    },
    # gai — generic AI proxy
    "gai": {
        "kind": "openai-compat",
        "tiers": frozenset({Tier.SIMPLE, Tier.STANDARD, Tier.COMPLEX}),
        "cost_per_1k": 0.001,
        "local": False,
    },
}


def _build_registry() -> dict[str, ProviderSpec]:
    """Build PROVIDER_REGISTRY from the canonical provider frozensets + metadata.

    All names in _LOCAL_OAI_PROVIDERS and _CLOUD_PROVIDERS are included.
    Additional local providers (ollama, llamacpp) that are not in either frozenset
    are added explicitly as they are the primary local dispatch targets.

    Any name in the frozensets that lacks explicit metadata gets a safe default
    so the registry stays in sync even when new providers are added upstream.
    """
    registry: dict[str, ProviderSpec] = {}

    # Canonical sets from core.agent
    all_cloud = _CLOUD_PROVIDERS
    all_local = _LOCAL_OAI_PROVIDERS

    # Extra local providers not in the frozensets
    extra_local: frozenset[str] = frozenset({"ollama", "llamacpp"})

    def _spec(name: str, is_local: bool) -> ProviderSpec:
        meta = _PROVIDER_META.get(name)
        if meta is None:
            log.warning("router.registry: no metadata for provider, using defaults", provider=name)
            meta = {
                "kind": "openai-compat",
                "tiers": frozenset({Tier.SIMPLE, Tier.STANDARD, Tier.COMPLEX}),
                "cost_per_1k": 0.0 if is_local else 0.001,
                "local": is_local,
            }
        return ProviderSpec(
            name=name,
            kind=meta["kind"],
            tiers=meta["tiers"],
            cost_per_1k=meta["cost_per_1k"],
            local=meta["local"],
        )

    for name in sorted(all_local):
        registry[name] = _spec(name, is_local=True)

    for name in sorted(extra_local):
        if name not in registry:
            registry[name] = _spec(name, is_local=True)

    for name in sorted(all_cloud):
        registry[name] = _spec(name, is_local=False)

    return registry


# ---------------------------------------------------------------------------
# Public registry — single source of truth for this module
# ---------------------------------------------------------------------------

PROVIDER_REGISTRY: dict[str, ProviderSpec] = _build_registry()


# ---------------------------------------------------------------------------
# available_providers — filter registry by what is actually configured
# ---------------------------------------------------------------------------


def available_providers(cfg: dict[str, Any]) -> list[str]:
    """Return registry provider names that are configured/available in *cfg*.

    *cfg* is the ``llm_agent_config`` dict (parsed JSON from the settings DB).
    Shape of cfg:
        {
            "provider":    str,          # active provider name
            "enabled":     bool,         # default True
            "api_key":     str,          # cloud API key
            "ollama_url":  str,
            "llamacpp_url": str,
            "ollama_model": str,
            ...
        }

    Rules:
    - If cfg is empty or ``enabled`` is False, return [].
    - The active ``provider`` is always included if it exists in the registry.
    - Local providers (ollama, llamacpp, shimmy, localai) are considered
      available when they appear as the configured provider.
    - Cloud providers are available when an ``api_key`` is set AND the provider
      key matches (i.e. configured provider is a cloud provider).
    - All cloud providers with a non-empty api_key in cfg are included when
      provider is a cloud provider (supports multi-provider cascades in future).
    """
    if not cfg:
        return []

    enabled: bool = cfg.get("enabled", True)
    if not enabled:
        return []

    active_provider: str = (cfg.get("provider") or "").strip()
    api_key: str = (cfg.get("api_key") or "").strip()

    result: list[str] = []
    seen: set[str] = set()

    def _add(name: str) -> None:
        if name in PROVIDER_REGISTRY and name not in seen:
            result.append(name)
            seen.add(name)

    # Always include the active provider first (if registered)
    if active_provider:
        _add(active_provider)

    # If the active provider is a cloud provider with an api_key, also expose
    # all other cloud providers that share the same key style (openrouter
    # multi-model, for example).  For now we include them all when any
    # cloud provider + key is configured — this gives the selector a richer
    # fallback chain.
    if active_provider in _CLOUD_PROVIDERS and api_key:
        for name in sorted(_CLOUD_PROVIDERS):
            _add(name)

    # Local providers are available when explicitly configured.
    if active_provider in _LOCAL_OAI_PROVIDERS or active_provider in {"ollama", "llamacpp"}:
        for name in sorted(_LOCAL_OAI_PROVIDERS | {"ollama", "llamacpp"}):
            _add(name)

    return result
