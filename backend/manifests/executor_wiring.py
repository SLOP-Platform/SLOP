"""backend/manifests/executor_wiring.py

App-to-app wiring pass, extracted from executor.py (#1302 linecount drain).

Records and attempts inter-app wiring connections (e.g. an *arr app pointing at
Prowlarr) declared via manifest ``post_deploy`` wire steps. ``run_wiring_pass``
runs after a batch install; ``run_pending_wiring`` is the health-scheduler retry
for deferred wires. Self-contained — depends only on ``StateDB``, ``time``,
``log`` and lazy imports (``WIRE_HANDLERS`` / ``load_all_manifests`` /
``OperationalLevel``); no other executor internal. ``executor.py`` re-exports
the public entry points so callers/tests resolve unchanged.
"""

from __future__ import annotations

import time
from typing import Any

from backend.core.logging import get_logger
from backend.core.state import StateDB

log = get_logger(__name__)


def _wire(source_key: str, target_key: str, wire_type: str) -> dict[str, str]:
    """Record a wiring connection. Actual API wiring implemented in Step 5."""
    with StateDB() as db:
        source = db.get_app(source_key)
        target = db.get_app(target_key)

    if source is None:
        return {
            "status": "skipped",
            "message": f"Wiring skipped — '{source_key}' is not installed.",
        }
    if target is None:
        return {
            "status": "skipped",
            "message": f"Wiring skipped — '{target_key}' is not installed.",
        }

    with StateDB() as db:
        db.execute(
            """INSERT OR IGNORE INTO wiring
               (source_app_id, target_app_id, wire_type, status, wired_at)
               VALUES (?,?,?,?,?)""",
            (source.id, target.id, wire_type, "active", int(time.time())),
        )

    return {
        "status": "ok",
        "message": f"Wired {source_key} → {target_key} ({wire_type}).",
    }


def _apply_wire_result(
    db: StateDB,
    source_app_id: int,
    target_app_id: int,
    wire_type: str,
    outcome: str,
) -> None:
    """Update a wiring row's status to reflect the wire_indexer outcome.

    "wired"    → status='active' (configuration confirmed in the target app)
    "deferred" → leave 'pending'  (scheduler retries on a later cycle)
    "failed"   → status='failed'  (surfaced to the UI; needs attention)
    """
    if outcome == "wired":
        db.execute(
            "UPDATE wiring SET status='active', wired_at=? "
            "WHERE source_app_id=? AND target_app_id=? AND wire_type=?",
            (int(time.time()), source_app_id, target_app_id, wire_type),
        )
    elif outcome == "failed":
        db.execute(
            "UPDATE wiring SET status='failed', checked_at=? "
            "WHERE source_app_id=? AND target_app_id=? AND wire_type=?",
            (int(time.time()), source_app_id, target_app_id, wire_type),
        )
    else:  # "deferred" — leave pending, just stamp the attempt
        db.execute(
            "UPDATE wiring SET checked_at=? "
            "WHERE source_app_id=? AND target_app_id=? AND wire_type=?",
            (int(time.time()), source_app_id, target_app_id, wire_type),
        )


def _dispatch_wire(
    source_key: str,
    wire_type: str,
    source_manifest: Any,
    target_key: str | None = None,
) -> str:
    """Route a wire_type to its registered handler. Returns the wire outcome.

    Dispatch is driven by WIRE_HANDLERS in backend/manifests/wiring.py — add
    a new wire_type there to extend without touching this function.

    Unknown wire types log an error and return "failed" so the wiring row is
    not retried forever on every health-scheduler cycle.  If the wire_type
    is not yet registered, the operator should either add a handler or
    remove the wiring declaration from the manifest.

    config_root is read from the platform record (not Config) — it is an
    operator-set, per-deployment value (see backend/core/state.Platform).

    target_key is forwarded to handlers that need it (e.g. download_client
    must know which arr app to configure).  Handlers that don't use it
    accept it as an ignored keyword argument.
    """
    from backend.manifests.wiring import WIRE_HANDLERS

    handler = WIRE_HANDLERS.get(wire_type)
    if handler is None:
        log.error(
            "Wiring: wire_type %r for app %r has no registered handler — "
            "add to WIRE_HANDLERS in backend/manifests/wiring.py",
            wire_type,
            source_key,
        )
        return "failed"
    with StateDB() as _db:
        config_root = _db.get_platform().config_root
    return handler(source_key, source_manifest, config_root, target_key)


def _reverse_wiring_pass(
    installed_keys: set[str],
    all_manifests: dict[str, Any],
    wired: list[str],
    deferred: list[str],
    failed: list[str],
) -> None:
    """Reverse pass sub-routine for run_wiring_pass.

    Checks previously-installed apps for wire steps targeting the newly-installed
    apps (installed_keys).  Closes the install-order gap: if app A was installed
    before app B, A's wire step targeting B was skipped at A's install time because
    B wasn't present yet.  When B installs (the current call), we look back at A
    and write any missing rows.

    Mutates the wired/deferred/failed lists in place.
    """
    with StateDB() as db:
        all_present_reverse = {a.key for a in db.get_all_apps()}
    previously_installed = all_present_reverse - installed_keys

    for existing_key in previously_installed:
        existing_manifest = all_manifests.get(existing_key)
        if existing_manifest is None:
            continue
        for step in existing_manifest.post_deploy:
            if step.step_type != "wire":
                continue
            target = step.target
            if target not in installed_keys:
                continue
            # Confirm both sides exist in the DB.
            with StateDB() as db:
                existing_app = db.get_app(existing_key)
                target_app_rev = db.get_app(target)
            if not (existing_app and target_app_rev):
                continue
            # Skip if a row already exists — INSERT OR IGNORE is safe here too,
            # but an explicit check lets us avoid a redundant dispatch entirely.
            with StateDB() as db:
                existing_row = db.execute(
                    "SELECT id FROM wiring "
                    "WHERE source_app_id=? AND target_app_id=? AND wire_type=?",
                    (existing_app.id, target_app_rev.id, step.wire_type),
                ).fetchone()
            if existing_row:
                continue
            # Write the missing row and attempt the wire.
            with StateDB() as db:
                db.execute(
                    """INSERT OR IGNORE INTO wiring
                       (source_app_id, target_app_id, wire_type, status, wired_at)
                       VALUES (?,?,?,?,?)""",
                    (
                        existing_app.id,
                        target_app_rev.id,
                        step.wire_type,
                        "pending",
                        int(time.time()),
                    ),
                )
            outcome = _dispatch_wire(
                existing_key,
                step.wire_type,
                existing_manifest,
                target_key=target,
            )
            with StateDB() as db:
                _apply_wire_result(
                    db,
                    existing_app.id,
                    target_app_rev.id,
                    step.wire_type,
                    outcome,
                )
            label = f"{existing_key}→{target}({step.wire_type})"
            if outcome == "wired":
                wired.append(label)
            elif outcome == "failed":
                failed.append(label)
            else:
                deferred.append(label)


def run_wiring_pass(installed_keys: set[str]) -> dict[str, list[str]]:
    """Write wiring rows for all apps in installed_keys whose manifest declares
    a wire dep that is also installed, then attempt the actual wiring.

    Called after a batch install completes. Safe to call multiple times —
    INSERT OR IGNORE prevents duplicate rows; wire attempts are idempotent.

    Rows are inserted with status='pending'. Each is then handed to the wire
    implementation: "wired" → 'active', "deferred" → stays 'pending' (the
    health scheduler retries via run_pending_wiring), "failed" → 'failed'.

    Returns {"wired": [...], "deferred": [...], "failed": [...], "skipped": [...]}.
    """
    from backend.manifests.loader import load_all_manifests

    wired: list[str] = []
    deferred: list[str] = []
    failed: list[str] = []
    skipped: list[str] = []
    all_manifests = load_all_manifests()
    with StateDB() as db:
        # Any app present in the DB (any status) is a valid wiring target — if the
        # target app isn't running yet, wire_indexer returns "deferred" and the
        # health scheduler retries. A status-based filter here would silently skip
        # wiring for stopped/unhealthy apps and never write the DB row, so the
        # scheduler would have nothing to retry.
        all_present = {a.key for a in db.get_all_apps()}

    for key in installed_keys:
        manifest = all_manifests.get(key)
        if not manifest:
            continue
        for step in manifest.post_deploy:
            if step.step_type != "wire":
                continue
            target = step.target
            if target not in all_present:
                skipped.append(f"{key}→{target} (target not installed)")
                continue
            with StateDB() as db:
                source_app = db.get_app(key)
                target_app = db.get_app(target)
            if not (source_app and target_app):
                continue
            # Record the intent first (status='pending') — the DB row is the
            # durable record; the actual configuration is a separate event.
            with StateDB() as db:
                db.execute(
                    """INSERT OR IGNORE INTO wiring
                       (source_app_id, target_app_id, wire_type, status, wired_at)
                       VALUES (?,?,?,?,?)""",
                    (source_app.id, target_app.id, step.wire_type, "pending", int(time.time())),
                )
            outcome = _dispatch_wire(key, step.wire_type, manifest, target_key=target)
            with StateDB() as db:
                _apply_wire_result(
                    db,
                    source_app.id,
                    target_app.id,
                    step.wire_type,
                    outcome,
                )
            label = f"{key}→{target}({step.wire_type})"
            if outcome == "wired":
                wired.append(label)
            elif outcome == "failed":
                failed.append(label)
            else:
                deferred.append(label)

    _reverse_wiring_pass(installed_keys, all_manifests, wired, deferred, failed)

    log.info(
        "Wiring pass: wired=%s deferred=%s failed=%s skipped=%s",
        wired,
        deferred,
        failed,
        skipped,
    )
    return {"wired": wired, "deferred": deferred, "failed": failed, "skipped": skipped}


def run_pending_wiring() -> dict[str, list[str]]:
    """Retry every wiring row still in status='pending'.

    Called by the health scheduler on each cycle — this is the asynchronous
    completion path for wiring that was deferred at install time (e.g. an arr
    app's config.xml hadn't been written yet). Idempotent and side-effect-safe:
    a row that wires successfully flips to 'active' and is no longer retried.

    Returns {"wired": [...], "deferred": [...], "failed": [...]} for logging.
    """
    from backend.manifests.loader import load_all_manifests
    from backend.agent.types import OperationalLevel

    wired: list[str] = []
    deferred: list[str] = []
    failed: list[str] = []

    with StateDB() as db:
        # #1252 Option A — honor the agent kill-switch (operational level). This retry
        # loop POSTs to external Prowlarr/*arr APIs on every scheduler cycle, autonomously
        # mutating the user's stack. ADVISORY means "propose, don't auto-execute" (the same
        # gate authorize() applies at governance.py:125), so the retry must pause while
        # autonomy is paused — rows stay 'pending' and resume when the level returns to
        # SUPERVISED/AUTONOMOUS. It deliberately does NOT consult the autofix rate-limit
        # budget (_budget_open): that budget is the remediation/autofix domain, and wiring
        # retries are reliability of user-authorized install work (#1252 decision memo:
        # docs/AGENT-1252-WIRING-RETRY-GOVERNANCE.md).
        if (
            OperationalLevel.from_setting(db.get_setting("agent_operational_level"))
            is OperationalLevel.ADVISORY
        ):
            log.info(
                "Pending wiring retry skipped: operational level ADVISORY "
                "(kill-switch engaged; rows remain pending)"
            )
            return {"wired": wired, "deferred": deferred, "failed": failed}
        pending = db.get_pending_wiring()
    if not pending:
        return {"wired": wired, "deferred": deferred, "failed": failed}

    all_manifests = load_all_manifests()
    for row in pending:
        source_key = row.get("source_key")
        wire_type = row.get("wire_type")
        source_app_id = row.get("source_app_id")
        target_app_id = row.get("target_app_id")
        if not (source_key and wire_type and source_app_id and target_app_id):
            continue
        manifest = all_manifests.get(source_key)
        target_key_val = row.get("target_key")
        outcome = _dispatch_wire(source_key, wire_type, manifest, target_key=target_key_val)
        with StateDB() as db:
            _apply_wire_result(
                db,
                source_app_id,
                target_app_id,
                wire_type,
                outcome,
            )
        label = f"{source_key}→{row.get('target_key')}({wire_type})"
        if outcome == "wired":
            wired.append(label)
        elif outcome == "failed":
            failed.append(label)
        else:
            deferred.append(label)

    log.info(
        "Pending wiring retry: wired=%s deferred=%s failed=%s",
        wired,
        deferred,
        failed,
    )
    return {"wired": wired, "deferred": deferred, "failed": failed}
