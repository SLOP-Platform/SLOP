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
SlotKind = Literal["stateful", "stateless"]

# How the rest of SLOP is wired to the active provider. Each value is the actual
# mechanism a provider in that slot uses today (cited per contract).
WiringKind = Literal[
    "forwardauth-labels",  # Traefik forwardAuth middleware labels (auth)
    "route-config",  # provider-owned external route config, e.g. CF Tunnel ingress
    "env-injection",  # env-var injection into other services
    "network-mode",  # docker network_mode: service:<container> (vpn)
    "labels-route",  # plain Traefik Host(...) router labels (dashboard/management)
]

# Cardinality of the slot — how many providers may be active at once.
#   one-active   — exactly one primary active; the rest of SLOP routes through it.
Cardinality = Literal["one-active"]


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


# Always-implemented lifecycle methods — these are abstract on InfraProvider, so
# every concrete provider necessarily overrides them. The contract's
# `required_methods` names the *slot-specific* verbs on top of these.
LIFECYCLE_METHODS: tuple[str, ...] = ("deploy", "remove", "verify")


# ── The five live slot contracts (each field cited to real provider code) ──────

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
            "not migrate it. Config-only redeploy via set_active. NOTE: several "
            "providers smuggle an upsert_app DB write into list_hostnames "
            "(management_portainer.py:127-147, management_alternatives.py) — a "
            "side-effect overload the C7 sentinel probe must not be fooled by."
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
}


# ── Derived accessors (the three implicit lists now reference THIS) ────────────


def all_slots() -> tuple[str, ...]:
    """The canonical slot set — replaces the three hand-maintained literal lists."""
    return tuple(SLOT_CONTRACTS.keys())


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
    "get_contract",
    "required_methods_for",
]
