"""backend/infra/slots.py

The single slot SSOT — `SLOT_CONTRACTS`.

This module is the **machine-checkable slot contract** (the keystone). Each
`SlotContract` declares, as *data*, what a slot is and what conformance means for
the providers that fill it. It collapses three previously-implicit slot lists
into one derived source:

  1. the `base.py` docstring's prose slot set,
  2. the `registry.py` import/registration list, and
  3. the `test_infra.py` literal slot tuple.

Every field is derived from the real provider code (cited in comments below), so
the contract names the constraint that already exists — it does not invent one.

The contract carries `slot_kind`, which selects which swap engine and which
conformance facets apply, **fail-closed**: a `stateless` slot does not pass by
silently omitting migration facets — it must explicitly declare them N/A
(`migration_pair=None`), or they run and go red.

Pure consolidation: importing this module has no runtime side effects on
providers and changes no behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

# ── Vocabulary (closed sets — every value below appears in live provider code) ──

# Which swap engine governs the slot.
#   stateful  — has migratable state (users, hostnames) → swap_slot 6-step engine.
#   stateless — config-only; a swap is a plain redeploy → set_active engine.
#   selection — NOT a host-mutating infra slot; a registry of simultaneously-
#               available providers selected per-request (the LLM router, #988).
#               No deploy/remove/migrate lifecycle; neither swap engine governs it.
SlotKind = Literal["stateful", "stateless", "selection"]

# How the rest of SLOP is wired to the active provider. Each value is the actual
# mechanism a provider in that slot uses today (cited per contract).
WiringKind = Literal[
    "forwardauth-labels",  # Traefik forwardAuth middleware labels (auth)
    "route-config",  # provider-owned external route config, e.g. CF Tunnel ingress
    "env-injection",  # env-var injection into other services
    "network-mode",  # docker network_mode: service:<container> (vpn)
    "labels-route",  # plain Traefik Host(...) router labels (dashboard/management)
    "api-router-chain",  # per-request router selection over a provider chain (llm)
]

# Cardinality of the slot — how many providers may be active at once.
#   one-active     — exactly one primary active; the rest of SLOP routes through it.
#   many-available — all registered providers are simultaneously available; the
#                    router selects per-request (no single "active" provider).
Cardinality = Literal["one-active", "many-available"]


@dataclass(frozen=True)
class HealthProbe:
    """Shape of the conformance health-probe expectation for a slot.

    `method` is the InfraProvider method that returns the health verdict
    (always `verify` today). `must_distinguish_unhealthy` records whether a
    conforming `verify()` must return a *failure* result when the underlying
    container is not running — i.e. whether a tautological `success()` is a
    conformance violation for this slot. This is the data that lets C6 ban the
    always-green probe.
    """

    method: str = "verify"
    must_distinguish_unhealthy: bool = True


@dataclass(frozen=True)
class MigrationPair:
    """The two halves of a state migration the swap engine drives for a slot.

    `export_method` is called on the OLD provider, `import_method` on the NEW
    provider (`registry._migrate_config`). Both must be implemented on a
    provider subclass for the slot's C8 migration round-trip to pass. `None` (the
    `migration_pair` being absent) means the slot is config-only — declared, not
    silently omitted.
    """

    export_method: str
    import_method: str


@dataclass(frozen=True)
class SlotContract:
    """The declared, machine-checkable contract for one infrastructure slot."""

    slot: str
    slot_kind: SlotKind
    wiring_kind: WiringKind
    cardinality: Cardinality
    # Methods a provider MUST override on its own class (cls.__dict__), beyond the
    # three abstract lifecycle methods every provider implements. These are the
    # slot-specific verbs whose *absence* the base class silently no-ops — the gap
    # the contract closes (C7).
    required_methods: tuple[str, ...]
    # The state-migration pair the swap engine drives, or None for config-only
    # (stateless) slots. None is an explicit declaration, never an omission.
    migration_pair: MigrationPair | None
    health_probe: HealthProbe = field(default_factory=HealthProbe)
    # Whether the C8 migration round-trip facet applies. Derived from slot_kind /
    # migration_pair but stored explicitly so a stateless slot's N/A is auditable
    # rather than inferred-and-forgotten (fail-closed: see conformance suite).
    migration_applies: bool = False
    # Whether the C9 swap-participation facet applies (the slot routes through the
    # stateful swap_slot engine). Stateless slots use set_active and declare False.
    swap_applies: bool = False
    notes: str = ""
    # For a `selection` slot only: the dotted ``module:attr`` path to the provider
    # registry (a ``dict[str, ProviderSpec]``) the slot selects over. Empty for
    # lifecycle (stateful/stateless) slots, which have no registry_ref. The
    # selection conformance facet (C11) resolves and validates it (fail-closed).
    registry_ref: str = ""


# Always-implemented lifecycle methods — these are abstract on InfraProvider, so
# every concrete provider necessarily overrides them. The contract's
# `required_methods` names the *slot-specific* verbs on top of these.
LIFECYCLE_METHODS: tuple[str, ...] = ("deploy", "remove", "verify")


# ── The slot contracts (each field cited to real provider code) ────────────────
# Six deployable lifecycle slots (auth/tunnel/vpn/management/dashboard/reverse_proxy)
# + one selection slot (llm, #988). deployable_slots()/selection_slots() partition them.

SLOT_CONTRACTS: dict[str, SlotContract] = {
    # auth — forwardAuth middleware labels.
    #   wiring: tinyauth labels `traefik.http.middlewares.tinyauth-auth.forwardauth.address`
    #           (auth_tinyauth.py:96-101); authelia `...middlewares.authelia.forwardauth.address`
    #           (auth_authelia.py:172-175).
    #   stateful: real user migration via export_users/import_users
    #           (base.py:82-88; registry._migrate_config auth branch :227-247).
    "auth": SlotContract(
        slot="auth",
        slot_kind="stateful",
        wiring_kind="forwardauth-labels",
        cardinality="one-active",
        required_methods=("protect", "unprotect", "export_users", "import_users"),
        migration_pair=MigrationPair(export_method="export_users", import_method="import_users"),
        health_probe=HealthProbe(method="verify", must_distinguish_unhealthy=True),
        migration_applies=True,
        swap_applies=True,
        notes=(
            "import_users is implemented by ZERO providers today (#974) — a real "
            "data-loss-on-swap bug. C8 correctly turns this RED; the burndown is "
            "net-new migration code, not a scaffold TODO."
        ),
    ),
    # tunnel — external route config.
    #   wiring: cloudflared owns CF Tunnel ingress + DNS via the CF API
    #           (tunnel_cloudflare.py:144-257 register_hostname); tailscale/headscale
    #           defer per-app hostnames to Traefik (tunnel_tailscale.py:156-165).
    #   stateful: hostname state migrates (list_hostnames -> re-register;
    #           registry._migrate_config tunnel branch :248-258, manual-warning).
    "tunnel": SlotContract(
        slot="tunnel",
        slot_kind="stateful",
        wiring_kind="route-config",
        cardinality="one-active",
        required_methods=("register_hostname", "unregister_hostname", "list_hostnames"),
        # Tunnel migration is list-then-manual-re-register; the engine's "export"
        # half is list_hostnames, and re-registration is register_hostname on the
        # new provider (registry._migrate_config tunnel branch).
        migration_pair=MigrationPair(
            export_method="list_hostnames", import_method="register_hostname"
        ),
        health_probe=HealthProbe(method="verify", must_distinguish_unhealthy=True),
        migration_applies=True,
        swap_applies=True,
        notes=(
            "Migration is list-hostnames + manual re-register (registry.py:248-258 "
            "emits a warning, not an automatic transfer). C8 asserts both halves "
            "exist; semantic transfer fidelity is a LIVE-tier concern."
        ),
    ),
    # vpn — docker network-mode.
    #   wiring: apps route via `network_mode: service:gluetun` (vpn_gluetun.py docstring
    #           + deploy fragment exposes proxy ports 8888/8388, NO Traefik labels).
    #   stateless: degenerate slot — only Gluetun exists; a swap is a plain redeploy.
    #           registry._migrate_config falls to the else branch :259-260 ("skipped").
    "vpn": SlotContract(
        slot="vpn",
        slot_kind="stateless",
        wiring_kind="network-mode",
        cardinality="one-active",
        required_methods=(),
        migration_pair=None,
        health_probe=HealthProbe(method="verify", must_distinguish_unhealthy=True),
        migration_applies=False,
        swap_applies=False,
        notes=(
            "Degenerate (Gluetun-only) slot. No migratable state — config-only "
            "redeploy via set_active. C8/C9 explicitly N/A (declared, not omitted)."
        ),
    ),
    # management — plain Traefik Host(...) router labels.
    #   wiring: portainer labels `traefik.http.routers.portainer.rule=Host(...)`
    #           (management_portainer.py:55-60); same shape for dockhand/dockge/komodo/
    #           portainer_be (management_alternatives.py).
    #   stateless: no migratable state — registry._migrate_config else branch "skipped".
    "management": SlotContract(
        slot="management",
        slot_kind="stateless",
        wiring_kind="labels-route",
        cardinality="one-active",
        required_methods=(),
        migration_pair=None,
        health_probe=HealthProbe(method="verify", must_distinguish_unhealthy=True),
        migration_applies=False,
        swap_applies=False,
        notes=(
            "Management UIs hold their own state in their own volumes; SLOP does "
            "not migrate it. Config-only redeploy via set_active. App registration "
            "(upsert_app) lives in each provider's deploy() — the canonical seam, "
            "matching every other infra provider; list_hostnames is a pure read. "
            "(#994 removed the prior upsert smuggled into the read-only "
            "list_hostnames that the C7 sentinel had to be hardened against.)"
        ),
    ),
    # dashboard — plain Traefik Host(...) router labels.
    #   wiring: homepage `traefik.http.routers.homepage.rule=Host(...)`
    #           (dashboard_homepage.py:67-70); glance same (dashboard_glance.py:90-93).
    #   stateless: dashboards hold config in their own YAML; no SLOP-driven migration.
    "dashboard": SlotContract(
        slot="dashboard",
        slot_kind="stateless",
        wiring_kind="labels-route",
        cardinality="one-active",
        required_methods=(),
        migration_pair=None,
        health_probe=HealthProbe(method="verify", must_distinguish_unhealthy=True),
        migration_applies=False,
        swap_applies=False,
        notes=(
            "Dashboards hold their layout in their own config files; SLOP does not "
            "migrate it. Config-only redeploy via set_active. C8/C9 N/A (declared)."
        ),
    ),
    # reverse_proxy — FOUNDATIONAL slot (other slots emit their route/forwardauth
    # labels THROUGH the active provider). Traefik is the first provider.
    #   wiring: the provider owns the raw `traefik.http.*` label emission seam
    #           (emit_route_labels/emit_forwardauth_labels). Today compose.py emits
    #           those labels directly (build_traefik_fragment:190 / _traefik_labels:584);
    #           the provider WRAPS that logic in P1 and BECOMES the sole emitter in P2
    #           (#990 staged inversion).
    #   stateless: config-only — Traefik's routing is materialized from compose labels +
    #           static traefik.yml, not a SLOP-migrated store. A swap is a plain redeploy
    #           (set_active). TLS/cert state (acme.json) is consumed BY-REFERENCE and is
    #           NOT in this contract's migration_pair (Traefik/Caddy cert formats differ —
    #           folding it risks ACME data loss; #990 design §Contract).
    "reverse_proxy": SlotContract(
        slot="reverse_proxy",
        slot_kind="stateless",
        wiring_kind="labels-route",
        cardinality="one-active",
        required_methods=("emit_route_labels", "emit_forwardauth_labels"),
        migration_pair=None,
        health_probe=HealthProbe(method="verify", must_distinguish_unhealthy=True),
        migration_applies=False,
        swap_applies=False,
        notes=(
            "Foundational reverse-proxy slot (#990). The active provider owns the raw "
            "Traefik label emission seam (emit_route_labels/emit_forwardauth_labels). "
            "Stateless: cert/ACME state is by-reference (migration_pair=None), so a swap "
            "is a redeploy via set_active. P1 (this) wires the provider additively "
            "(it wraps compose.py); P2 inverts compose.py to emit BY-REFERENCE through it."
        ),
    ),
    # llm — registry-backed SELECTION slot (#988), NOT a host-mutating infra slot.
    #   wiring: the LLM router selects a provider per request over a fallback chain
    #           (backend/agent/router/registry.py PROVIDER_REGISTRY → selector.py);
    #           there is no Traefik label / network wiring and no deploy/remove.
    #   selection: ~14 providers all simultaneously available; most are SaaS with
    #           no lifecycle. Conforms via the selection-kind contract + C11 registry
    #           validation, not an InfraProvider subclass. No migratable state.
    "llm": SlotContract(
        slot="llm",
        slot_kind="selection",
        wiring_kind="api-router-chain",
        cardinality="many-available",
        required_methods=(),
        migration_pair=None,
        health_probe=HealthProbe(method="verify", must_distinguish_unhealthy=True),
        migration_applies=False,
        swap_applies=False,
        registry_ref="backend.agent.router.registry:PROVIDER_REGISTRY",
        notes=(
            "Selection slot (NOT a host-mutating infra slot). The LLM router's "
            "providers (PROVIDER_REGISTRY) are all simultaneously available as a "
            "per-request fallback chain — no deploy/remove/migrate, most are SaaS. "
            "Conforms to the slot framework via the selection-kind contract + C11 "
            "registry validation (tests/test_provider_conformance.py), NOT an "
            "InfraProvider subclass (#988). Never persisted as an infra_slots DB "
            "row (migration 001 CHECK excludes it); deployable_slots() omits it so "
            "it never surfaces in the infra deploy API/UI. health_probe is inert "
            "here (no provider exercises C6)."
        ),
    ),
}


# ── Derived accessors (the three implicit lists now reference THIS) ────────────


def all_slots() -> tuple[str, ...]:
    """The canonical slot set — replaces the three hand-maintained literal lists.

    This is the FULL SSOT (deployable + selection). Infra-deploy consumers that
    must exclude registry-backed selection slots use ``deployable_slots()``.
    """
    return tuple(SLOT_CONTRACTS.keys())


def deployable_slots() -> tuple[str, ...]:
    """Host-mutating infra slots (deploy/swap-managed) — every slot EXCEPT
    selection slots. This is the set the infra deploy API/UI surface and that
    migration 001's ``infra_slots`` seed + CHECK enumerate; a selection slot
    (e.g. ``llm``) is registry-backed and never persisted/deployed, so it is
    excluded. Use this — not ``all_slots()`` — wherever the operand is a
    deployable slot, so a selection slot can never leak into the deploy surface."""
    return tuple(s for s, c in SLOT_CONTRACTS.items() if c.slot_kind != "selection")


def selection_slots() -> tuple[str, ...]:
    """Registry-backed selection slots (``slot_kind == "selection"``) — providers
    are simultaneously available and selected per-request, with no deploy
    lifecycle. The complement of ``deployable_slots()`` within ``all_slots()``."""
    return tuple(s for s, c in SLOT_CONTRACTS.items() if c.slot_kind == "selection")


def get_contract(slot: str) -> SlotContract:
    """Return the contract for `slot`, raising KeyError if it is not a known slot."""
    return SLOT_CONTRACTS[slot]


def required_methods_for(slot: str) -> tuple[str, ...]:
    """Slot-specific methods a provider in `slot` must override on its own class."""
    return SLOT_CONTRACTS[slot].required_methods


__all__ = [
    "LIFECYCLE_METHODS",
    "SLOT_CONTRACTS",
    "Cardinality",
    "HealthProbe",
    "MigrationPair",
    "SlotContract",
    "SlotKind",
    "WiringKind",
    "all_slots",
    "deployable_slots",
    "get_contract",
    "required_methods_for",
    "selection_slots",
]
