"""backend.agent.router.types — Core data shapes for the LLM routing engine.

Tier is an IntEnum so that ordering comparisons work naturally:
    Tier.SIMPLE < Tier.STANDARD < Tier.COMPLEX < Tier.REASONING

selector.py relies on Tier ordering to cap at req.max_tier.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Tier(enum.IntEnum):
    """Complexity tier for an LLM routing decision.

    Values are ordered lowest-to-highest so that ``<=`` and ``>=`` comparisons
    work naturally.  Assign explicit ints to keep them stable across renames.
    """

    SIMPLE = 1
    STANDARD = 2
    COMPLEX = 3
    REASONING = 4


@dataclass
class ProviderSpec:
    """Static description of a single LLM provider registered in the router.

    Attributes:
        name:         Canonical provider key (matches keys in PROVIDER_REGISTRY).
        kind:         Human-readable kind label, e.g. ``"ollama"``, ``"openai"``.
        tiers:        Set of Tier values this provider is suited for.
        cost_per_1k:  USD cost per 1 000 tokens (0.0 for local/free providers).
        local:        True if the provider runs on the local host (no API key required).
    """

    name: str
    kind: str
    tiers: frozenset[Tier]
    cost_per_1k: float
    local: bool


@dataclass
class RouteRequest:
    """Input to the routing engine.

    Attributes:
        prompt:    The full prompt text to be routed.
        max_tier:  Upper bound on the tier the caller is willing to use.
                   Defaults to REASONING (no cap).
    """

    prompt: str
    max_tier: Tier = field(default=Tier.REASONING)


@dataclass
class RouteDecision:
    """Output from the routing engine.

    Attributes:
        tier:   The tier selected for this request.
        chain:  Ordered list of provider names to try (primary first, fallbacks after).
        reason: Human-readable explanation of the decision.
    """

    tier: Tier
    chain: list[str]
    reason: str
