"""backend.agent.router.decisions — Decision logging for the LLM routing engine.

Emits a structured log event for each RouteDecision so that routing choices
are observable via the structlog pipeline, and best-effort persists a row to
the ``router_decisions`` table.

The database write is always best-effort: any DB error is caught, logged as a
warning, and swallowed so the caller is never blocked.  Existing single-arg
callers (e.g. the CLI dry-run path) continue to work unchanged — the extra
kwargs are all optional.
"""

from __future__ import annotations

import json

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
    """Emit the structlog event (unchanged) AND best-effort insert a
    router_decisions row.  Never raises.  Existing single-arg callers
    (cli.py dry-run) keep working — extra kwargs are optional.

    Fields emitted to structlog:
        event:           "router.decision"
        tier:            Tier name string, e.g. "SIMPLE"
        chain:           List of provider names in dispatch order
        reason:          Human-readable explanation from the selector
        chosen_provider: Provider that responded (or None)
        outcome:         'success' | 'all_failed' | None

    DB columns written (best-effort, never raises):
        prompt_chars, tier, chain (JSON), chosen_provider, outcome,
        cost_usd, latency_ms, created_at (default unixepoch())
    """
    # ── 1. Emit the structlog event (unchanged from batch 1) ─────────────────
    try:
        log.info(
            "router.decision",
            tier=decision.tier.name,
            chain=decision.chain,
            reason=decision.reason,
            chosen_provider=chosen_provider,
            outcome=outcome,
        )
    except Exception as exc:  # pragma: no cover — defensive only
        log.warning("router.decisions: failed to log decision", error=str(exc))

    # ── 2. Best-effort DB insert ──────────────────────────────────────────────
    try:
        from backend.core.state import StateDB  # lazy import; avoids hard dep at module load

        prompt_chars = len(decision.chain)  # rough proxy when prompt text unavailable
        chain_json = json.dumps(decision.chain)

        with StateDB() as db:
            db.execute(
                """
                INSERT INTO router_decisions
                    (prompt_chars, tier, chain, chosen_provider, outcome, cost_usd, latency_ms)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    prompt_chars,
                    decision.tier.name,
                    chain_json,
                    chosen_provider,
                    outcome,
                    cost_usd,
                    latency_ms,
                ),
            )
    except Exception as exc:
        log.warning("router.decisions: failed to persist decision row", error=str(exc))
