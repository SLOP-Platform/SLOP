"""backend/agent/function_registry.py — agent-function introspection runtime accessor.

The A1 component from AGENT-FULL-FUNCTIONALITY-PLAN: the runtime accessor that
makes the agent's function surface enumerable and queryable.  The agent can
enumerate its own probes + actions so a slot-introspection consumer (or #991's
AI-driven onboarding) can reason about what the agent can observe and do.

LAYERING:
  * This module wraps the unified registry (``backend.agent.registry``) — it
    derives its data FROM the registry, never duplicates it.
  * ``list_actions`` is re-exported from the registry; ``list_probes`` wraps
    ``probe_specs()``; ``agent_surface()`` gives the unified picture.
  * No executor imports — purely metadata, safe to import anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.agent.registry import (
    KIND_PROBE,
    list_actions,
    probe_specs,
)
from backend.agent.types import ActionTier
from backend.core.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Read-only projections — consumable by chat / MCP / #991.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProbeView:
    """Read-only projection of a probe entry for a client (chat / MCP / #991)."""

    id: str
    kind: str = KIND_PROBE
    tier: int = int(ActionTier.INVESTIGATE)  # T0
    description: str = ""


@dataclass(frozen=True)
class AgentFunctionSurface:
    """The agent's complete self-knowledge: all probes + all actions.

    This is the structured answer to "what can the agent do?" — it carries
    no handler references, only metadata safe for the agent to reason over."""

    probes: list[ProbeView] = field(default_factory=list)
    actions: list[Any] = field(default_factory=list)  # ActionView from registry

    def probe_ids(self) -> list[str]:
        return [p.id for p in self.probes]

    def action_ids(self) -> list[str]:
        return [a.id for a in self.actions]


# ---------------------------------------------------------------------------
# Public API — the PINNED introspection seam.
# ---------------------------------------------------------------------------


def list_probes() -> list[ProbeView]:
    """Return every registered probe as a read-only :class:`ProbeView`.

    Sources from ``probe_specs()`` — the @register_probe self-registration
    in ``backend/agent/registry.py``, populated once ``backend/health/scheduler.py``
    has been imported.
    """
    return [ProbeView(id=m.id, description=m.description) for m in probe_specs()]


def agent_surface() -> AgentFunctionSurface:
    """Return the unified agent function surface: all probes + all actions.

    This is the entrypoint an AI-driven onboarding consumer (#991) uses to
    enumerate the agent's vocabulary — what it can observe AND what it can do.
    """
    return AgentFunctionSurface(
        probes=list_probes(),
        actions=list_actions(),
    )


# Some consumers may want just the raw spec rows (for tooling/manifests).
list_agent_actions = list_actions
list_agent_probes = list_probes


__all__ = [
    "AgentFunctionSurface",
    "ProbeView",
    "agent_surface",
    "list_agent_actions",
    "list_agent_probes",
    "list_probes",
]
