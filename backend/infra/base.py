"""backend/infra/base.py

Infrastructure slot provider interface.

Every infrastructure provider (Tinyauth, Authelia, Cloudflare Tunnel, etc.)
implements this interface. The swap system uses it to migrate from one
provider to another without the executor caring which provider is active.

Providers are grouped into slots. The canonical slot set and the per-slot
contract (slot_kind, wiring_kind, required_methods, migration_pair, health_probe,
cardinality) are declared as data in `backend/infra/slots.py:SLOT_CONTRACTS` —
the single slot SSOT. This docstring no longer maintains its own slot list;
the authoritative set is (auth / tunnel / vpn / management / dashboard /
reverse_proxy) — see that module for what each slot's contract requires.

Each slot can have zero or one active provider. The swap system:
  1. Deploys the new provider alongside the old one
  2. Migrates configuration (users, routes, etc.)
  3. Switches traffic to the new provider (one service at a time for auth)
  4. Verifies everything works
  5. Removes the old provider
  6. Rolls back to the old provider if any step fails
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any


@dataclass
class ProviderResult:
    ok: bool
    message: str
    detail: str = ""
    data: dict[str, Any] | None = None

    @classmethod
    def success(cls, message: str, data: dict[str, Any] | None = None) -> ProviderResult:
        return cls(ok=True, message=message, data=data)

    @classmethod
    def failure(cls, message: str, detail: str = "") -> ProviderResult:
        return cls(ok=False, message=message, detail=detail)


class InfraProvider(ABC):
    """Base interface every infrastructure provider must implement."""

    slot: str = ""  # which slot this provider fills
    key: str = ""  # unique provider key e.g. "tinyauth"
    display_name: str = ""  # human label e.g. "Tinyauth v5"

    # ── Lifecycle ─────────────────────────────────────────────────────────

    @abstractmethod
    def deploy(self, config: dict[str, Any]) -> ProviderResult:
        """Deploy this provider. Idempotent — safe to call on already-running."""
        ...

    @abstractmethod
    def remove(self) -> ProviderResult:
        """Stop and remove this provider's containers and fragments."""
        ...

    @abstractmethod
    def verify(self) -> ProviderResult:
        """Verify the provider is working correctly."""
        ...

    # ── Auth slot interface (only auth providers implement) ───────────────

    def protect(self, hostname: str, rules: dict[str, Any] | None = None) -> ProviderResult:
        """Register a hostname with this auth provider."""
        return ProviderResult.success("Not applicable for this provider type.")

    def unprotect(self, hostname: str) -> ProviderResult:
        """Remove auth protection from a hostname."""
        return ProviderResult.success("Not applicable for this provider type.")

    def export_users(self) -> ProviderResult:
        """Export user list for migration to another auth provider."""
        return ProviderResult.failure("User export not supported by this provider.")

    def import_users(self, users: list[dict[str, Any]]) -> ProviderResult:
        """Import users from another auth provider."""
        return ProviderResult.failure("User import not supported by this provider.")

    # ── Tunnel slot interface (only tunnel providers implement) ───────────

    def register_hostname(self, hostname: str, target: str) -> ProviderResult:
        """Register a public hostname pointing to a service."""
        return ProviderResult.success("Not applicable for this provider type.")

    def unregister_hostname(self, hostname: str) -> ProviderResult:
        """Remove a public hostname."""
        return ProviderResult.success("Not applicable for this provider type.")

    def list_hostnames(self) -> ProviderResult:
        """List all registered hostnames."""
        return ProviderResult.success("Not applicable.", data={"hostnames": []})

    # ── Reverse-proxy slot interface (only reverse_proxy providers implement) ──
    # These emit the raw reverse-proxy routing labels for an app. The base no-ops
    # return an empty label list so a non-reverse_proxy provider contributes nothing;
    # the active reverse_proxy provider (e.g. Traefik) overrides them with the real
    # `traefik.http.*` emission (the #990 seam — wrapped in P1, the sole emitter in P2).

    def emit_route_labels(self, *args: Any, **kwargs: Any) -> list[str]:
        """Emit the Host(...)/router/service labels that publish an app. Base no-op."""
        return []

    def emit_forwardauth_labels(self, *args: Any, **kwargs: Any) -> list[str]:
        """Emit the forwardAuth-middleware labels wiring an app to the auth slot. Base no-op."""
        return []

    # ── Migration support ─────────────────────────────────────────────────

    def pre_migration_snapshot(self) -> ProviderResult:
        """Capture current state for rollback. Called before swap begins."""
        return ProviderResult.success("No snapshot required.", data={})

    def restore_from_snapshot(self, snapshot: dict[str, Any]) -> ProviderResult:
        """Restore provider to a previous state. Called on rollback."""
        return ProviderResult.success("No restore required.")
