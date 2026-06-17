"""backend/infra/registry.py

Infrastructure provider registry and slot swap engine.

Usage:
    from backend.infra.registry import get_provider, swap_slot

    provider = get_provider("auth", "tinyauth")
    result = provider.deploy(config)

    swap_result = swap_slot("auth", from_provider="tinyauth",
                            to_provider="authelia", config={...})
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from backend.core.logging import get_logger
from backend.core.state import StateDB
from backend.infra.base import InfraProvider
from backend.infra.slots import SLOT_CONTRACTS

log = get_logger(__name__)

# ── Provider registry ─────────────────────────────────────────────────────

_REGISTRY: dict[tuple[str, str], type[InfraProvider]] = {}


def register(provider_cls: type[InfraProvider]) -> type[InfraProvider]:
    """Decorator to register a provider class."""
    key = (provider_cls.slot, provider_cls.key)
    _REGISTRY[key] = provider_cls
    return provider_cls


def get_provider(slot: str, key: str) -> InfraProvider:
    """Return an instantiated provider for the given slot and key.

    Raises KeyError if the provider is not registered.
    """
    _ensure_providers_registered()
    cls = _REGISTRY.get((slot, key))
    if cls is None:
        available = [k for s, k in _REGISTRY if s == slot]
        raise KeyError(f"No provider '{key}' for slot '{slot}'. Available: {available or ['none']}")
    return cls()


def list_providers(slot: str | None = None) -> list[dict[str, str]]:
    _ensure_providers_registered()
    """List all registered providers, optionally filtered by slot."""
    result = []
    for (s, k), cls in _REGISTRY.items():
        if slot is None or s == slot:
            result.append(
                {
                    "slot": s,
                    "key": k,
                    "display_name": cls.display_name,
                }
            )
    return sorted(result, key=lambda x: (x["slot"], x["key"]))


# ── Swap result ───────────────────────────────────────────────────────────


@dataclass
class SwapStep:
    name: str
    status: str  # ok | error | skipped | rolled_back
    message: str
    detail: str = ""


@dataclass
class SwapResult:
    ok: bool
    slot: str
    from_provider: str
    to_provider: str
    steps: list[SwapStep] = field(default_factory=list)
    rolled_back: bool = False
    error: str = ""

    def add(self, name: str, status: str, message: str, detail: str = "") -> None:
        self.steps.append(SwapStep(name, status, message, detail))
        if status == "error" and not self.error:
            self.error = message


# ── Swap engine ───────────────────────────────────────────────────────────


def swap_slot(
    slot: str,
    from_provider_key: str,
    to_provider_key: str,
    config: dict[str, Any],
) -> SwapResult:
    """Swap an infrastructure slot from one provider to another.

    The swap is atomic from the user's perspective:
      1. Snapshot the current provider's state (for rollback)
      2. Deploy the new provider alongside the old one
      3. Migrate configuration (users, routes, etc.) from old to new
      4. Verify the new provider is working
      5. Remove the old provider
      6. Update the slot state in the DB

    If any step 2-5 fails, automatically roll back to the old provider.
    """
    result = SwapResult(
        ok=True,
        slot=slot,
        from_provider=from_provider_key,
        to_provider=to_provider_key,
    )

    # Record migration in DB
    migration_id: int | None = None
    try:
        old_provider = get_provider(slot, from_provider_key)
        new_provider = get_provider(slot, to_provider_key)
    except KeyError as e:
        result.ok = False
        result.error = str(e)
        result.add("lookup", "error", str(e))
        return result

    with StateDB() as db:
        cur = db._c.execute(
            """INSERT INTO infra_migrations
               (slot, from_provider, to_provider, status, steps_total, started_at)
               VALUES (?,?,?,?,?,?)""",
            (slot, from_provider_key, to_provider_key, "started", 5, int(time.time())),
        )
        migration_id = cur.lastrowid

    snapshot: dict[str, Any] = {}

    try:
        # ── Step 1: Snapshot ──────────────────────────────────────────────
        snap = old_provider.pre_migration_snapshot()
        if snap.ok:
            snapshot = snap.data or {}
            result.add("snapshot", "ok", "Current state captured for rollback.")
        else:
            result.add(
                "snapshot",
                "warning",
                "Could not capture rollback snapshot — proceeding with caution.",
                snap.detail,
            )

        _update_migration(migration_id, steps_completed=1, current_step="deploy_new")

        # ── Step 2: Deploy new provider ───────────────────────────────────
        deploy = new_provider.deploy(config)
        if not deploy.ok:
            result.add("deploy_new", "error", f"Could not deploy {to_provider_key}.", deploy.detail)
            result.ok = False
            result.error = deploy.message
            _complete_migration(migration_id, "failed", result.error)
            return result
        result.add("deploy_new", "ok", deploy.message)

        _update_migration(migration_id, steps_completed=2, current_step="migrate_config")

        # ── Step 3: Migrate configuration ─────────────────────────────────
        migrate = _migrate_config(slot, old_provider, new_provider)
        result.add(
            "migrate_config", migrate["status"], migrate["message"], migrate.get("detail", "")
        )
        if migrate["status"] == "error":
            result.ok = False
            result.error = migrate["message"]
            _rollback(result, old_provider, snapshot, migration_id)
            return result

        _update_migration(migration_id, steps_completed=3, current_step="verify_new")

        # ── Step 4: Verify new provider ───────────────────────────────────
        verify = new_provider.verify()
        if not verify.ok:
            result.add(
                "verify_new", "error", f"{to_provider_key} is not working correctly.", verify.detail
            )
            result.ok = False
            result.error = verify.message
            _rollback(result, old_provider, snapshot, migration_id)
            return result
        result.add("verify_new", "ok", verify.message)

        _update_migration(migration_id, steps_completed=4, current_step="remove_old")

        # ── Step 5: Remove old provider ───────────────────────────────────
        remove = old_provider.remove()
        if not remove.ok:
            result.add(
                "remove_old",
                "warning",
                f"Old provider ({from_provider_key}) could not be fully removed.",
                remove.detail,
            )
        else:
            result.add("remove_old", "ok", remove.message)

        _complete_migration(migration_id, "completed")
        result.add("complete", "ok", f"Slot '{slot}' now uses {new_provider.display_name}.")

    except Exception as e:
        result.ok = False
        result.error = f"Unexpected error during swap: {e}"
        result.add("unexpected", "error", result.error, str(e))
        log.exception("Unexpected error swapping %s → %s", from_provider_key, to_provider_key)
        _rollback(result, old_provider, snapshot, migration_id)

    return result


# ── Stateless engine ──────────────────────────────────────────────────────


@dataclass
class SetActiveResult:
    ok: bool
    slot: str
    from_provider: str
    to_provider: str
    steps: list[SwapStep] = field(default_factory=list)
    error: str = ""

    def add(self, name: str, status: str, message: str, detail: str = "") -> None:
        self.steps.append(SwapStep(name, status, message, detail))
        if status == "error" and not self.error:
            self.error = message


def set_active(slot: str, key: str, config: dict[str, Any]) -> SetActiveResult:
    """Activate a provider in a **stateless** slot — config-only, no migration.

    Stateless slots (slot_kind == "stateless": vpn, management, dashboard) hold no
    SLOP-migratable state. Switching providers is a plain redeploy: remove the
    current provider (if any), then deploy the new one. There is no
    snapshot → deploy-alongside → migrate → verify → remove dance — that ceremony
    exists only to protect migratable state, which these slots do not have.

    The contract's `slot_kind` selects this engine over `swap_slot`. Calling
    `set_active` on a stateful slot is a programming error and is rejected
    fail-closed (use `swap_slot`, which preserves users / hostnames).
    """
    result = SetActiveResult(ok=True, slot=slot, from_provider="", to_provider=key)

    contract = SLOT_CONTRACTS.get(slot)
    if contract is None:
        result.ok = False
        result.add("lookup", "error", f"Unknown slot '{slot}'.")
        return result
    if contract.slot_kind != "stateless":
        # Fail-closed: refuse to config-swap a slot that carries state. The whole
        # point of the two-engine split is that a stateful slot routes through the
        # migration-preserving swap_slot, never this shortcut.
        result.ok = False
        result.add(
            "guard",
            "error",
            f"Slot '{slot}' is stateful — use swap_slot() so its state is migrated, "
            "not set_active().",
        )
        return result

    try:
        new_provider = get_provider(slot, key)
    except KeyError as e:
        result.ok = False
        result.add("lookup", "error", str(e))
        return result

    # Determine and remove the currently-active provider (if any).
    current_key = ""
    try:
        with StateDB() as db:
            current = db.get_slot(slot)
        current_key = getattr(current, "provider", "") or ""
    except Exception as e:  # ground-truth read is best-effort; deploy still proceeds
        log.debug("set_active: could not read current %s provider: %s", slot, e)

    result.from_provider = current_key

    if current_key and current_key != key:
        try:
            old_provider = get_provider(slot, current_key)
            remove = old_provider.remove()
            result.add(
                "remove_old",
                "ok" if remove.ok else "warning",
                remove.message,
                remove.detail,
            )
        except KeyError:
            result.add("remove_old", "warning", f"No provider class for current '{current_key}'.")

    deploy = new_provider.deploy(config)
    if not deploy.ok:
        result.ok = False
        result.error = deploy.message
        result.add("deploy_new", "error", deploy.message, deploy.detail)
        return result
    result.add("deploy_new", "ok", deploy.message)
    result.add("complete", "ok", f"Slot '{slot}' now uses {new_provider.display_name}.")
    return result


def _migrate_config(slot: str, old: InfraProvider, new: InfraProvider) -> dict[str, str]:
    """Migrate configuration from old to new provider for the given slot."""
    if slot == "auth":
        # Export users from old, import into new. FAIL-CLOSED on data loss (#974):
        # any condition where users EXIST but cannot be fully migrated returns
        # "error" so swap_slot rolls back, rather than the old silent "warning"/
        # "skipped" that let a swap complete while dropping every user.
        export = old.export_users()
        if not export.ok:
            # Export itself failed — we cannot know if users would be lost. Treat as
            # an error so the swap rolls back instead of silently proceeding.
            return {
                "status": "error",
                "message": f"Could not export users from {old.display_name}: {export.message}",
                "detail": export.detail,
            }
        data = export.data or {}
        users = data.get("users", [])
        if not users:
            # Genuinely no users to migrate (empty deployment) — safe to proceed.
            return {
                "status": "ok",
                "message": "No users to migrate (none configured).",
            }
        if data.get("lossy"):
            # Export declared it cannot carry the full user set — do NOT silently
            # drop them. Roll back and surface why to the operator.
            return {
                "status": "error",
                "message": (
                    f"User migration would lose data: {data.get('lossy_reason') or 'lossy export'}. "
                    "Swap aborted to protect users."
                ),
                "detail": data.get("lossy_reason", ""),
            }
        imp = new.import_users(users)
        if not imp.ok:
            return {
                "status": "error",
                "message": f"User migration failed — swap aborted: {imp.message}",
                "detail": imp.detail,
            }
        return {
            "status": "ok",
            "message": f"Migrated {len(users)} user(s) to {new.display_name}.",
        }
    elif slot == "tunnel":
        # Hostname migration is manual — just report what needs to be done
        hostnames = old.list_hostnames()
        hn_list = (hostnames.data or {}).get("hostnames", [])
        if hn_list:
            return {
                "status": "warning",
                "message": f"{len(hn_list)} hostname(s) need to be re-registered in the new tunnel.",
                "detail": f"Hostnames: {', '.join(hn_list)}",
            }
        return {"status": "ok", "message": "No hostnames to migrate."}
    else:
        return {"status": "skipped", "message": f"No config migration defined for slot '{slot}'."}


def _rollback(
    result: SwapResult,
    old_provider: InfraProvider,
    snapshot: dict[str, Any],
    migration_id: int | None,
) -> None:
    """Attempt to restore the old provider from snapshot."""
    log.warning("Rolling back infra swap — restoring %s", old_provider.key)
    try:
        restore = old_provider.restore_from_snapshot(snapshot)
        if restore.ok:
            result.add(
                "rollback", "ok", f"Rolled back to {old_provider.display_name} successfully."
            )
        else:
            result.add(
                "rollback",
                "error",
                "Rollback failed — manual intervention required.",
                restore.detail,
            )
    except Exception as e:
        result.add("rollback", "error", "Rollback encountered an unexpected error.", str(e))
    result.rolled_back = True
    if migration_id:
        _complete_migration(migration_id, "rolled_back")


def _update_migration(migration_id: int | None, steps_completed: int, current_step: str) -> None:
    if migration_id is None:
        return
    try:
        with StateDB() as db:
            db._c.execute(
                """UPDATE infra_migrations
                   SET steps_completed=?, current_step=? WHERE id=?""",
                (steps_completed, current_step, migration_id),
            )
    except Exception:  # noqa: S110  # best-effort migration progress tracking; never fatal
        pass


def _complete_migration(migration_id: int | None, status: str, error: str | None = None) -> None:
    if migration_id is None:
        return
    try:
        with StateDB() as db:
            db._c.execute(
                """UPDATE infra_migrations
                   SET status=?, completed_at=? WHERE id=?""",
                (status, int(time.time()), migration_id),
            )
    except Exception:  # noqa: S110  # best-effort migration completion tracking; never fatal
        pass


# ── Auto-register providers ───────────────────────────────────────────────
# Import providers here to trigger the @register decorator.
# Providers self-register via @register decorator when imported.
# Call _ensure_providers_registered() before using list_providers() or get_provider()
# if providers may not have been imported yet.


def _ensure_providers_registered() -> None:
    """Import all provider modules and register them — idempotent."""
    if ("auth", "tinyauth") in _REGISTRY:
        return  # already registered
    # pylint: disable=import-outside-toplevel
    from backend.infra.providers.auth_tinyauth import TinyauthProvider
    from backend.infra.providers.tunnel_cloudflare import CloudflareTunnelProvider
    from backend.infra.providers.tunnel_tailscale import TailscaleProvider
    from backend.infra.providers.vpn_gluetun import GluetunProvider
    from backend.infra.providers.dashboard_homepage import HomepageProvider
    from backend.infra.providers.dashboard_glance import GlanceDashboardProvider
    from backend.infra.providers.management_portainer import PortainerProvider
    from backend.infra.providers.auth_authelia import AutheliaProvider
    from backend.infra.providers.tunnel_headscale import HeadscaleProvider
    from backend.infra.providers.management_alternatives import (
        DockhandProvider,
        DockgeProvider,
        KomodoProvider,
        PortainerBEProvider,
    )

    # The imports above are what trigger each provider class definition. The
    # registration loop iterates those same symbols directly — there is no longer
    # a separate hand-maintained class list to drift out of sync with the imports
    # (one of the three implicit slot lists the SLOT_CONTRACTS SSOT collapses).
    _provider_classes = (
        TinyauthProvider,
        CloudflareTunnelProvider,
        TailscaleProvider,
        GluetunProvider,
        HomepageProvider,
        GlanceDashboardProvider,
        PortainerProvider,
        DockhandProvider,
        DockgeProvider,
        KomodoProvider,
        PortainerBEProvider,
        AutheliaProvider,
        HeadscaleProvider,
    )
    for cls in _provider_classes:
        # Fail-closed: a provider whose slot is not a declared contract slot is a
        # registration error, not a silent add (the SLOT_CONTRACTS SSOT is the
        # authority on what slots exist).
        if cls.slot not in SLOT_CONTRACTS:
            raise ValueError(
                f"Provider {cls.__name__} declares unknown slot '{cls.slot}'. "
                f"Known slots (backend/infra/slots.py): {sorted(SLOT_CONTRACTS)}"
            )
        # registry stores classes; instantiation is gated by get_provider().
        _REGISTRY[(cls.slot, cls.key)] = cls
