"""backend.agent.router — Complexity-tiered LLM routing engine.

Public re-exports for the most commonly used names.
Consumers should import directly from the submodule when possible.
"""

from backend.agent.router.types import (
    Tier,
    ProviderSpec,
    RouteRequest,
    RouteDecision,
)
from backend.agent.router.registry import PROVIDER_REGISTRY, available_providers

__all__ = [
    "PROVIDER_REGISTRY",
    "ProviderSpec",
    "RouteDecision",
    "RouteRequest",
    "Tier",
    "available_providers",
]
