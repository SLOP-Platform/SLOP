"""Single agent↔API outcome-recording seam (#822).

Before this seam, only the manual *approve* path (``backend/api/health.py``)
computed and stored a ``signature_hash`` + ``diagnosis_class`` on its
``fix_history`` row. The *reject* path and BOTH auto-apply paths
(``backend/agent/apply.py`` ``_mark_applied`` / ``_mark_failed``) inserted rows
with ``signature_hash = NULL``, which ``StateDB.learning_outcome_tally`` — keyed
on ``WHERE signature_hash = ?`` — can never see. The agent's own outcomes (the
bulk of the learning signal) therefore never fed back: the "learning loop is
write-only" defect #822 names.

``record_fix_outcome`` is the ONE place every fix outcome is written to
``fix_history``. It derives the ``signature_hash`` with the SAME recipe the
approve path and the classify/cache-read path use (``classify_offline`` →
``compute_signature_hash``), so a stored outcome and the next occurrence's
cache lookup agree on the key. Callers keep their existing ``error_type`` /
``context`` field mapping (passed in) for backward continuity; only the two
learning keys are centralised here so they cannot silently diverge again.
"""

from __future__ import annotations

import time
from typing import Any

from backend.agent.classifier import classify_offline, compute_signature_hash

# Canonical vocabulary for ``fix_history.outcome`` — the SSOT (AGENT-MEMORY-DESIGN.md
# §7 "Outcome vocabulary correct", #1212). GROUND-derived from every writer of the
# column: this seam (caller-decided label, below), the auto-apply paths
# (``backend/agent/apply.py`` → ``success`` / ``failed_verification``), post-fix
# re-check (``backend/health/fix_verification.py`` → ``success`` / ``failed_verification``),
# the approve/reject API paths (``backend/api/health.py`` → ``success`` /
# ``user_approved_manual`` / ``pending`` / ``failure``), the
# ``PUT /fix-history/{id}/outcome`` route (constrained to ``success`` / ``failure``)
# and the ``POST /fix-history`` route (``backend/api/models.py`` ``record_fix`` — a
# direct INSERT now validated against this set, #1212).
#
# Why this constant is the SSOT and NOT the schema comment: ``backend/core/schema.sql``
# is a GENERATED dump of the migrations, and ``fix_history`` is created in the BASELINE
# migration ``migrations/001_baseline.sql`` whose SHA-256 is checksum-pinned in
# ``schema_migrations`` (``backend/core/migrations.py`` → ``MigrationChecksumMismatch``,
# proven-red by ``tests/test_migrations.py::test_checksum_tampering_raises``). Editing the
# baseline — even a comment — would break every already-migrated install, so the stale
# ``-- pending | success | failure`` comment there cannot be safely corrected; the live
# vocabulary is pinned HERE instead and reconciled by ``tests/test_fix_outcome_vocabulary.py``.
FIX_HISTORY_OUTCOMES: frozenset[str] = frozenset(
    {
        "pending",
        "success",
        "failure",
        "failed_verification",
        "user_approved_manual",
    }
)


def record_fix_outcome(
    db: Any,
    *,
    app_key: str,
    problem: str,
    error_type: str,
    context: str,
    suggested_fix: str,
    outcome: str,
    image_digest: str = "",
    rejection_reason: str = "",
) -> int:
    """Insert one ``fix_history`` row carrying the learning-store keys.

    Args:
        db: An OPEN ``StateDB`` (the caller owns the transaction / commit).
        app_key: Catalog key of the affected app — part of the signature.
        problem: Raw error/problem text the ``signature_hash`` is derived from
            (the pending_fixes ``problem`` column at every call site).
        error_type: Value stored in ``fix_history.error_type`` (historically the
            action_type or the diagnosis_class — preserved per-caller).
        context: Value stored in ``fix_history.context`` (problem text or
            fix_type — preserved per-caller).
        suggested_fix: The remediation that was approved/rejected/applied.
        outcome: ``success`` / ``failure`` / ``failed_verification`` /
            ``user_approved_manual`` / ``pending`` — caller decides the label;
            this seam does not reinterpret it.
        image_digest: Image digest for version-aware reconciliation. Defaults to
            ``""`` (the column's ``NOT NULL DEFAULT ''``) — every existing call
            site left it unset, so this preserves prior behaviour exactly.

    Returns:
        The ``rowid`` of the inserted ``fix_history`` row (so the caller can
        record an explicit reference — the #822 referential link, Unit B).
    """
    err_class = classify_offline(problem)
    sig_hash = compute_signature_hash(err_class, problem, app_key)
    cur = db.execute(
        """INSERT INTO fix_history
               (app_key, error_type, context, suggested_fix, outcome, created_at,
                diagnosis_class, signature_hash, image_digest, rejection_reason)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            app_key,
            error_type,
            context,
            suggested_fix,
            outcome,
            int(time.time()),
            err_class.value,
            sig_hash,
            image_digest,
            rejection_reason,
        ),
    )
    return int(cur.lastrowid)
