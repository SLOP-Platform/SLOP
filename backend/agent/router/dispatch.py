"""backend.agent.router.dispatch — Wire the routing engine into live dispatch.

``route_and_dispatch`` selects a complexity-appropriate provider chain via the
router (``select`` + ``available_providers``) and dispatches the call with
free/local-first bounded fallback.  EVERY per-provider call is routed through
``backend.health.checker._dispatch_llm_call`` so the ``scrub()`` choke
point (ADR-0021) is preserved unchanged — there is deliberately NO second
egress path in this module.

Graceful degrade is the overriding contract: if the router or its config is
unavailable, raises, or yields an empty chain, this falls back to today's exact
single-provider ``_dispatch_llm_call`` behavior so the agent never gets *worse*
at diagnosing because of routing.

v1 reuses the caller-supplied ``model`` / ``ollama_url`` for every provider in
the chain (no per-provider model matrix — see the wave's "Out of scope").
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import structlog

from backend.agent.router.types import Tier

log = structlog.get_logger(__name__)

# ── Budget admission control (#826) ──────────────────────────────────────────
# The 24h-budget pre-flight was a bare read-check: under concurrent dispatch, N
# calls could all read spend < budget BEFORE any of them recorded spend, then
# jointly overspend. The fix is race-free admission: a single asyncio.Lock
# serializes the check-and-reserve, and an in-process counter of in-flight
# reservations is added to the committed 24h spend so concurrent callers see
# each other's intended spend. The reservation is a conservative per-call
# estimate, released in a finally once the call completes.

# Conservative completion-size used to estimate a call's cost for reservation.
_RESERVE_COMPLETION_TOKENS = 1024
# Floor so every admitted call reserves a strictly-positive amount — this is
# what bounds concurrency even when provider cost tables are empty/unknown.
_RESERVE_FLOOR = 1e-6


class _BudgetGate:
    """Process-wide admission gate for budget-mode dispatch.

    ``lock`` serializes the check-and-reserve; ``reserved`` is the sum of
    in-flight per-call reservations (committed spend lives in the DB).
    """

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.reserved = 0.0


_gate = _BudgetGate()


def _committed_spend_24h() -> float:
    """Rolling 24h cloud spend from cloud_llm_usage (0.0 on any failure)."""
    try:
        from backend.core.state import StateDB

        since = int(time.time()) - 86400
        with StateDB() as db:
            row = db.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) as spend "
                "FROM cloud_llm_usage WHERE created_at >= ?",
                (since,),
            ).fetchone()
        return float(row["spend"]) if row else 0.0
    except Exception as exc:
        log.warning("router.dispatch: budget spend query failed; assuming 0", error=str(exc))
        return 0.0


def _estimate_call_cost(available: list[str], prompt: str) -> float:
    """Conservative worst-case cost estimate for one call, for reservation.

    Reserves the most expensive *cloud* provider currently available (fail-
    closed: over-reserving rejects, which is the safe direction for a hard cap).
    Always returns at least ``_RESERVE_FLOOR`` so the reservation is positive.
    """
    try:
        from backend.agent.router.selector import _is_free
        from backend.core.cloud_llm import estimate_cost

        prompt_tokens = max(1, len(prompt) // 4)
        cloud = [p for p in available if not _is_free(p)]
        costs = [estimate_cost(p, prompt_tokens, _RESERVE_COMPLETION_TOKENS) for p in cloud]
        est = max(costs) if costs else 0.0
    except Exception:
        est = 0.0
    return max(est, _RESERVE_FLOOR)


async def route_and_dispatch(
    client: Any,
    prompt: str,
    cfg: dict[str, Any],
    *,
    ollama_url: str,
    model: str,
    api_key: str,
    cloud_providers: set[str],
    max_tier: Tier = Tier.REASONING,
) -> str:
    """Select a provider chain and dispatch with free/local-first fallback.

    Behavior:
    - Build a :class:`RouteRequest` from *prompt* / *max_tier*, compute the
      available providers from *cfg*, and ask the router to ``select`` a chain.
    - EMPTY chain (or any router/config failure) → legacy single-provider
      ``_dispatch_llm_call`` using ``cfg['provider']`` so nothing regresses.
    - Otherwise try each provider in ``decision.chain`` at most once, in order,
      stopping on the first success.  Each attempt goes through
      ``_dispatch_llm_call(provider=name, ...)`` so scrub still applies
      per-provider.
    - On first success → ``log_decision(decision, outcome='success',
      chosen_provider=name, latency_ms=...)`` and return the text.
    - On exhaustion of the whole chain → ``log_decision(decision,
      outcome='all_failed')`` and return ``''``.

    Never raises: any unexpected failure degrades to the legacy path.
    """
    # Deferred imports: avoid circular dependency with backend.health.checker
    # and keep router/selector optional at module load.
    from backend.health.checker import _dispatch_llm_call

    legacy_provider = (cfg or {}).get("provider", "ollama")

    async def _legacy() -> str:
        """Today's exact single-provider behavior — the degrade target."""
        return await _dispatch_llm_call(
            client,
            prompt,
            ollama_url,
            legacy_provider,
            api_key,
            model,
            cloud_providers,
        )

    # Budget-mode reservation state: released in the finally on EVERY return
    # path once this call has been admitted (reserved against the gate).
    _reservation = 0.0
    _admitted = False
    try:
        # ── 1. Build the routing decision (graceful degrade on any failure) ──
        try:
            from backend.agent.router.registry import available_providers
            from backend.agent.router.selector import select, _is_free
            from backend.agent.router.types import RouteRequest

            available = available_providers(cfg or {})

            # ── Budget enforcement ────────────────────────────────────────────
            # Load budget settings from llm_agent_config blob (defaults: 0.0 / ["ollama"]).
            try:
                from backend.core.state import StateDB as _StateDB
                import json as _json

                with _StateDB() as _db:
                    _raw = _db.get_setting("llm_agent_config")
                    _agent_cfg = _json.loads(_raw) if _raw else {}
            except Exception:
                _agent_cfg = {}

            _llm_budget: float = float(_agent_cfg.get("llm_budget", 0.0))
            _free_tier_priority: list[str] = _agent_cfg.get("free_tier_priority", ["ollama"])

            if _llm_budget == 0.0:
                # Free-only mode: restrict available providers to free/local ones.
                available = [p for p in available if _is_free(p)]
                log.debug("router.dispatch: free-only mode; available=%s", available)
            else:
                # Budget mode: race-free admission. Serialize check-and-reserve so
                # concurrent dispatches can't all pass a stale pre-flight read and
                # jointly overspend (#826). Committed 24h spend + in-flight
                # reservations are compared against the cap under one lock.
                # Per-call spend IS now recorded (#1115): _call_cloud_provider
                # records each router-dispatched cloud call to cloud_llm_usage, so
                # _committed_spend_24h() reflects sustained sequential load too —
                # the cap is durable against both concurrent AND sequential spend.
                _reservation = _estimate_call_cost(available, prompt)
                async with _gate.lock:
                    _spend_24h = _committed_spend_24h()
                    if _spend_24h + _gate.reserved >= _llm_budget:
                        log.warning(
                            "router.dispatch: 24h budget exceeded "
                            "(spend=%.4f + reserved=%.4f >= budget=%.2f); "
                            "returning INDETERMINATE",
                            _spend_24h,
                            _gate.reserved,
                            _llm_budget,
                        )
                        return ""
                    # Admit: reserve this call's estimated cost so concurrent
                    # callers see it before any spend is recorded.
                    _gate.reserved += _reservation
                    _admitted = True

            decision = select(RouteRequest(prompt=prompt, max_tier=max_tier), available)
        except Exception as exc:
            log.warning("router.dispatch: selection failed; using legacy path", error=str(exc))
            return await _legacy()

        # ── 2. Empty chain → legacy single-provider path (no regression) ─────
        if not decision.chain:
            log.debug("router.dispatch: empty chain; using legacy single-provider path")
            return await _legacy()

        # ── 3. Bounded free/local-first fallback over the chain ──────────────
        start = time.monotonic()
        for name in decision.chain:
            try:
                raw = await _dispatch_llm_call(
                    client,
                    prompt,
                    ollama_url,
                    name,
                    api_key,
                    model,
                    cloud_providers,
                )
            except Exception as exc:
                log.debug(
                    "router.dispatch: provider failed; trying next", provider=name, error=str(exc)
                )
                continue
            # Success on the first provider that returns.
            from backend.agent.router.decisions import log_decision

            log_decision(
                decision,
                chosen_provider=name,
                outcome="success",
                latency_ms=int((time.monotonic() - start) * 1000),
            )
            return raw

        # ── 4. Whole chain exhausted ─────────────────────────────────────────
        from backend.agent.router.decisions import log_decision

        log_decision(
            decision,
            outcome="all_failed",
            latency_ms=int((time.monotonic() - start) * 1000),
        )
        return ""
    finally:
        # Release this call's reservation once it has finished (success, legacy
        # degrade, or chain exhaustion). Only set when budget-mode admitted.
        if _admitted:
            async with _gate.lock:
                _gate.reserved -= _reservation
