"""backend.agent.router.selector — Tier selection + free-first fallback chain.

``select(req, available)`` is the heart of the routing engine.  Given a
:class:`RouteRequest` and the list of currently-available provider names (from
``registry.available_providers``), it returns a :class:`RouteDecision`:

    1. tier  = min(complexity_score(req.prompt), req.max_tier)
       Tier is an IntEnum, so ``min`` respects the SIMPLE<STANDARD<COMPLEX<
       REASONING ordering and caps the scored tier at the caller's ceiling.

    2. chain = ordered list of providers that can serve ``tier``, sorted
       free/local-first then by ascending cost:
           sort key = (cost_bucket, cost_per_1k, name)
       where cost_bucket is 0 for local-or-free providers (local=True OR
       cost_per_1k == 0.0) and 1 for paid providers.  ``name`` is the final
       tie-break so the output is fully deterministic.

    3. Degrade path: if NO available provider serves ``tier``, we step the tier
       down one level at a time (COMPLEX→STANDARD→SIMPLE) and retry, recording
       the degrade in ``reason``.  If even SIMPLE has no provider, the chain is
       empty and ``reason`` explains that nothing is available.

Inputs are never mutated.  No I/O, no randomness — deterministic for a given
(prompt, max_tier, available) triple.  ADR-0010: explicit logic only.
"""

from __future__ import annotations

import structlog

from backend.agent.router.registry import PROVIDER_REGISTRY
from backend.agent.router.scoring import complexity_score
from backend.agent.router.types import RouteDecision, RouteRequest, Tier

log = structlog.get_logger(__name__)

# Tiers from highest to lowest, used to walk down the degrade path.
_TIERS_DESC: tuple[Tier, ...] = (Tier.REASONING, Tier.COMPLEX, Tier.STANDARD, Tier.SIMPLE)


def _is_free(name: str) -> bool:
    """True if provider *name* is local or zero-cost (cost bucket 0)."""
    spec = PROVIDER_REGISTRY[name]
    return spec.local or spec.cost_per_1k == 0.0


def _chain_for_tier(tier: Tier, candidates: list[str]) -> list[str]:
    """Ordered, deduplicated provider chain that serves *tier*.

    Only names present in PROVIDER_REGISTRY whose ProviderSpec.tiers includes
    *tier* are kept.  Ordering: (cost_bucket, cost_per_1k, name).
    """
    seen: set[str] = set()
    eligible: list[str] = []
    for name in candidates:
        if name in seen:
            continue
        seen.add(name)
        spec = PROVIDER_REGISTRY.get(name)
        if spec is None:
            continue
        if tier in spec.tiers:
            eligible.append(name)

    eligible.sort(
        key=lambda n: (
            0 if _is_free(n) else 1,
            PROVIDER_REGISTRY[n].cost_per_1k,
            n,
        )
    )
    return eligible


def select(req: RouteRequest, available: list[str]) -> RouteDecision:
    """Return a :class:`RouteDecision` for *req* given *available* providers.

    See the module docstring for the tier cap, chain ordering, and degrade rule.
    """
    scored = complexity_score(req.prompt)
    tier = min(scored, req.max_tier)

    capped_note = ""
    if scored > req.max_tier:
        capped_note = f" (capped from {scored.name} by max_tier={req.max_tier.name})"

    # Try the chosen tier, then degrade downward until we find providers.
    tiers_to_try = [t for t in _TIERS_DESC if t <= tier]
    for idx, attempt_tier in enumerate(tiers_to_try):
        chain = _chain_for_tier(attempt_tier, available)
        if chain:
            if idx == 0:
                reason = (
                    f"tier={attempt_tier.name}{capped_note}; "
                    f"{len(chain)} provider(s), free/local first then by cost: "
                    f"{', '.join(chain)}"
                )
            else:
                reason = (
                    f"tier={tier.name}{capped_note} had no available provider; "
                    f"degraded to {attempt_tier.name}; "
                    f"{len(chain)} provider(s): {', '.join(chain)}"
                )
            decision = RouteDecision(tier=attempt_tier, chain=chain, reason=reason)
            log.debug(
                "router.select",
                scored=scored.name,
                selected=attempt_tier.name,
                chain=chain,
            )
            return decision

    # Nothing serves the chosen tier or any lower tier.
    reason = (
        f"tier={tier.name}{capped_note}; no available provider serves this tier "
        f"or any lower tier (available={list(available)}); empty chain"
    )
    log.warning(
        "router.select.no_provider", scored=scored.name, tier=tier.name, available=list(available)
    )
    return RouteDecision(tier=tier, chain=[], reason=reason)
