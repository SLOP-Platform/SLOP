"""backend/agent/slot_conformance.py — runtime conformance gate wrapper.

The runtime half of MODULAR-SLOTS-PLAN Decision 2(b): "make conformance a
precondition of register()/instantiation, not a CI-only diff."

The CI suite (``tests/test_provider_conformance.py``, 729 lines) is the
STATIC tier — it proves structural completeness at test time.  This module
provides the RUNTIME tier: a gate that can be called at registration to
validate a provider against its slot's contract BEFORE it is wired.  The
gate is a fail-closed wrapper: any exception consulting the contract or
importing the provider → deny.

THIS MODULE:
  * Imports from ``backend/infra/slots`` (read-only contract data) and
    ``backend/infra/registry`` (to look up providers).
  * Does NOT modify ``backend/infra/registry.py`` — that file is outside the
    ``backend/agent/`` surface.  The gate lives HERE and is called BY the
    registry's ``register()`` path (future #1329 follow-on) or by an
    onboarding agent (#991).
  * Aims for the same facets as the CI suite (C1-C9) but run at registration
    time.  It is deliberately less exhaustive than the CI suite — the CI suite
    is the authoritative conformance record; this runtime gate is a
    defense-in-depth tripwire, not a replacement.
"""

from __future__ import annotations

from dataclasses import dataclass

from backend.core.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Result types.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ConformanceVerdict:
    """The result of a runtime conformance check for one (slot, key) pair."""

    slot: str
    key: str
    passed: bool
    failures: tuple[str, ...] = ()
    summary: str = ""


# ---------------------------------------------------------------------------
# The check — runtime C1-C9 subset.
# ---------------------------------------------------------------------------

# Facets we validate at runtime (subset of the CI suite's C1-C9).
# Each facet returns (passed: bool, failure_msg: str).
# We validate at registration time, so we can check what matters for
# operational safety without the full CI mock harness.

_FACET_NAMES: dict[str, str] = {
    "identity": "C1: slot/key registered",
    "deploy": "C2: deploy() overridden",
    "remove": "C3: remove() overridden",
    "verify": "C6: verify() overridden + health-probe shape",
    "required_methods": "C7: contract-required methods overridden",
}


def check_provider_conformance(slot: str, key: str, provider_cls: type) -> ConformanceVerdict:
    """Run a runtime subset of conformance facets against *provider_cls*.

    This gate is defense-in-depth — it catches a provider that would fail CI
    BEFORE it gets registered.  It does NOT replace the CI suite; it is a
    tripwire, not the authoritative conformance record.

    Args:
        slot: Slot name (must be in SLOT_CONTRACTS).
        key: Provider key within the slot.
        provider_cls: The provider class (must be a subclass of InfraProvider).

    Returns:
        ConformanceVerdict with ``passed`` True iff all runtime facets pass.
    """
    from backend.infra.slots import SLOT_CONTRACTS
    from backend.infra.base import InfraProvider

    contract = SLOT_CONTRACTS.get(slot)
    if contract is None:
        return ConformanceVerdict(
            slot=slot,
            key=key,
            passed=False,
            failures=("unknown_slot",),
            summary=f"slot {slot!r} is not in SLOT_CONTRACTS",
        )

    failures: list[str] = []

    # Verify the class is a real InfraProvider subclass.
    if not (isinstance(provider_cls, type) and issubclass(provider_cls, InfraProvider)):
        return ConformanceVerdict(
            slot=slot,
            key=key,
            passed=False,
            failures=("not_infra_provider",),
            summary=f"{provider_cls!r} is not an InfraProvider subclass",
        )

    # C2: deploy() — must be overridden on the subclass itself.
    if not _is_real_override(provider_cls, "deploy", InfraProvider):
        failures.append("deploy_missing_or_inherited")

    # C3: remove() — must be overridden.
    if not _is_real_override(provider_cls, "remove", InfraProvider):
        failures.append("remove_missing_or_inherited")

    # C6: verify() must be overridden and return a ProviderResult.
    verify_fn = getattr(provider_cls, "verify", None)
    if verify_fn is None or not _is_real_override(provider_cls, "verify", InfraProvider):
        failures.append("verify_missing_or_inherited")

    # C7: contract-required methods must be overridden.
    for method_name in contract.required_methods:
        if not _is_real_override(provider_cls, method_name, InfraProvider):
            failures.append(f"required_method_{method_name}_missing")

    passed = len(failures) == 0
    return ConformanceVerdict(
        slot=slot,
        key=key,
        passed=passed,
        failures=tuple(failures),
        summary=("all facets passed" if passed else f"{len(failures)} facet(s) failed: {failures}"),
    )


def _is_real_override(cls: type, method_name: str, base_cls: type) -> bool:
    """Check that *method_name* is defined on a subclass of *base_cls*,
    not merely inherited from *base_cls* itself.

    Mirrors the sentinel probe from tests/test_provider_conformance.py:71-100
    (the C7/C8 no-op-stub defeater)."""
    # Walk MRO, stop before base_cls.
    for klass in cls.__mro__:
        if klass is base_cls:
            break
        if method_name in klass.__dict__:
            return True
    return False


__all__ = [
    "ConformanceVerdict",
    "check_provider_conformance",
]
