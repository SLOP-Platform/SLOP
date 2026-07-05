"""backend.agent.router.decisions — Decision logging for the LLM routing engine.

Emits a structured log event for each RouteDecision so that routing choices
(tier, chain, chosen provider, outcome, cost, latency) are observable via the
structlog pipeline.

The ``router_decisions`` DB leg was removed (#1090): the row was written on
every dispatch but had ZERO readers (no dashboard, no query), so it was dead
write amplification — and it carried a bug (``prompt_chars`` recorded
``len(chain)``, the provider count, not the prompt size). Observability now
rides structlog alone, which already covered routing and now also carries the
cost/latency fields the table held. The orphaned table itself is dropped in a
follow-up migration (its removal cascades through schema-regen + the generated
behavioral suite). Existing callers are unchanged — the kwargs stay optional.
"""

from __future__ import annotations

import structlog

from backend.agent.router.types import RouteDecision

log = structlog.get_logger(__name__)


def log_decision(
    decision: RouteDecision,
    *,
    chosen_provider: str | None = None,
    outcome: str | None = None,  # 'success' | 'all_failed' | None
    cost_usd: float | None = None,
    latency_ms: int | None = None,
) -> None:
    """Emit the structlog event for a RouteDecision. Never raises. Existing
    single-arg callers (cli.py dry-run) keep working — extra kwargs are optional.

    Fields emitted to structlog:
        event:           "router.decision"
        tier:            Tier name string, e.g. "SIMPLE"
        chain:           List of provider names in dispatch order
        reason:          Human-readable explanation from the selector
        chosen_provider: Provider that responded (or None)
        outcome:         'success' | 'all_failed' | None
        cost_usd:        Estimated cost in USD (or None)
        latency_ms:      Wall-clock dispatch latency in ms (or None)
    """
    try:
        log.info(
            "router.decision",
            tier=decision.tier.name,
            chain=decision.chain,
            reason=decision.reason,
            chosen_provider=chosen_provider,
            outcome=outcome,
            cost_usd=cost_usd,
            latency_ms=latency_ms,
        )
    except Exception as exc:  # pragma: no cover — defensive only
        log.warning("router.decisions: failed to log decision", error=str(exc))
