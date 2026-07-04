"""backend/core/state_aux.py

Auxiliary-table StateDB methods, extracted from state.py (#1302 linecount drain).

These cover the non-core tables — background jobs, probe-failure tracking,
spine advisories, port reservations, and the evidence-ranked learning shadow.
They are provided to ``StateDB`` as a mixin (``StateDB(_StateAuxMixin)``); the
mixin reuses ``StateDB``'s connection (``_c``), retrying ``execute``, ``_now``,
and ``_json_load`` helpers via MRO at runtime. Those helpers are declared under
``TYPE_CHECKING`` here purely so the type checker resolves ``self.<helper>`` —
they do NOT exist on the mixin at runtime (StateDB provides the real ones).
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import sqlite3

# ── Port-reservation staleness window ──────────────────────────────────────
# A port_reservations row older than this is a crash leftover, not a live hold,
# and may be reclaimed by another caller. It MUST be >= the install lock window
# (executor.MAX_INSTALL_SECONDS = 600s) so a legitimately slow install (a large
# image pull) never has its reservation expire mid-deploy and get its port
# stolen by a concurrent install (#1100 adversarial-review finding #3). The
# invariant is asserted at import in executor.py against MAX_INSTALL_SECONDS.
PORT_RESERVATION_STALE_MINUTES: int = 10


class _StateAuxMixin:
    """Auxiliary-table methods mixed into ``StateDB``.

    The attributes/methods below are provided by ``StateDB`` at runtime via the
    MRO; they are declared here (TYPE_CHECKING only) so mypy resolves the
    ``self.<helper>`` calls without a runtime override of StateDB's real ones.
    """

    if TYPE_CHECKING:

        @property
        def _c(self) -> sqlite3.Connection: ...

        def execute(self, sql: str, params: tuple[Any, ...] = ()) -> sqlite3.Cursor: ...

        def _now(self) -> int: ...

        def _json_load(self, value: str | None) -> dict[str, Any]: ...

    def create_job(self, job_id: str, kind: str, status: str, payload: dict[str, Any]) -> None:
        """Insert (or replace) a job row. Idempotent on job_id; preserves
        the original created_at on replace."""
        now = self._now()
        self.execute(
            "INSERT OR REPLACE INTO jobs (id, kind, status, payload, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, "
            "  COALESCE((SELECT created_at FROM jobs WHERE id = ?), ?), ?)",
            (job_id, kind, status, json.dumps(payload), job_id, now, now),
        )
        self._c.commit()

    def update_job(
        self,
        job_id: str,
        *,
        status: str | None = None,
        payload_patch: dict[str, Any] | None = None,
    ) -> None:
        """Update a job's status and/or shallow-merge keys into its payload.

        No-op if the job row does not exist (the in-memory dict remains the
        source of truth for liveness; this is a best-effort mirror).
        """
        row = self.execute("SELECT payload FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            return
        payload = self._json_load(row["payload"])
        if payload_patch:
            payload.update(payload_patch)
        now = self._now()
        if status is not None:
            self.execute(
                "UPDATE jobs SET status = ?, payload = ?, updated_at = ? WHERE id = ?",
                (status, json.dumps(payload), now, job_id),
            )
        else:
            self.execute(
                "UPDATE jobs SET payload = ?, updated_at = ? WHERE id = ?",
                (json.dumps(payload), now, job_id),
            )
        self._c.commit()

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        """Return {id, kind, status, payload, created_at, updated_at} or None.

        `payload` is the decoded dict (not the raw JSON string).
        """
        row = self.execute(
            "SELECT id, kind, status, payload, created_at, updated_at FROM jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "kind": row["kind"],
            "status": row["status"],
            "payload": self._json_load(row["payload"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def mark_running_jobs_unknown(self) -> int:
        """Flip any still-'running' job to 'unknown' (called once at startup).

        A job left 'running' across a restart cannot be resumed — its thread is
        gone. It is surfaced as 'unknown' (NOT 'error', NOT auto-restarted) so
        the frontend can offer a retry. Also stamps payload.status='unknown' so
        the opaque payload the polling endpoint returns stays consistent.
        Returns the number of rows updated.
        """
        rows = self.execute("SELECT id, payload FROM jobs WHERE status = 'running'").fetchall()
        now = self._now()
        for row in rows:
            payload = self._json_load(row["payload"])
            payload["status"] = "unknown"
            self.execute(
                "UPDATE jobs SET status = 'unknown', payload = ?, updated_at = ? WHERE id = ?",
                (json.dumps(payload), now, row["id"]),
            )
        self._c.commit()
        return len(rows)

    # ── Probe failures (health scheduler consecutive-failure tracking) ────

    def write_probe_failure(self, probe_name: str, fail_count: int, last_error: str) -> None:
        """Called by the health scheduler when a probe hits 5 consecutive failures."""
        import time as _time

        now = int(_time.time())
        self.execute(
            """INSERT OR REPLACE INTO probe_failures
               (probe_name, fail_count, last_error, last_failed_at)
               VALUES (?,?,?,?)""",
            (probe_name, fail_count, last_error, now),
        )

    def get_probe_failures(self) -> list[dict[str, Any]]:
        rows = self.execute("SELECT * FROM probe_failures ORDER BY last_failed_at DESC").fetchall()
        return [dict(r) for r in rows]

    def get_spine_advisories(self, limit: int = 100) -> list[dict[str, Any]]:
        """Read the store-only ``spine_advisories`` table — advisory LLM
        annotations persisted alongside the GROUND verdicts for later human
        review (migration 010 declares it store-only). The read surface for
        #1089 (the table was written by ``_persist_spine_advisories`` but had no
        consumer). Newest first; the ``annotation`` JSON column is parsed to an
        object (left as raw text if it ever fails to parse — never raises)."""
        import json as _json

        rows = self.execute(
            "SELECT id, finding_id, verdict, annotation, provider, created_at "
            "FROM spine_advisories ORDER BY created_at DESC, id DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        out: list[dict[str, Any]] = []
        for r in rows:
            rec = dict(r)
            try:
                rec["annotation"] = (
                    _json.loads(rec["annotation"]) if rec.get("annotation") else None
                )
            except (TypeError, ValueError):
                pass  # malformed JSON stays as raw text — a read surface never raises
            out.append(rec)
        return out

    # ── Port reservations (TOCTOU guard for replace_app) ─────────────────

    def reserve_port(self, port: int, key: str) -> None:
        """Write a DB-level port reservation before any container operation.

        Must be called by replace_app before stopping the old container so that
        _check_port_conflict blocks concurrent callers racing for the same port.
        reserved_at is stored as an ISO-8601 UTC string so that julianday() SQL
        arithmetic in _check_port_conflict works correctly.
        """
        import datetime as _dt

        now_iso = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%S")
        self._c.execute(
            """INSERT OR REPLACE INTO port_reservations (port, key, status, reserved_at)
               VALUES (?, ?, 'reserved', ?)""",
            (port, key, now_iso),
        )
        self._c.commit()

    def try_reserve_port(self, port: int, key: str) -> bool:
        """Atomically claim *port* for *key*, closing the install-vs-install
        TOCTOU (#1100). Returns True iff *key* holds the reservation afterwards.

        Unlike :meth:`reserve_port` (INSERT OR REPLACE — last writer always
        wins; used by replace_app to reserve a port it ALREADY owns), this is a
        *conditional* upsert: it takes the row only when the port is free,
        already held by *key*, or held by a STALE (>5min) reservation — never
        when a live reservation by a DIFFERENT key exists. Because ``port`` is
        the PRIMARY KEY and SQLite serializes writers, two concurrent installs
        racing for the same computed port resolve to exactly one winner; the
        loser gets False and fails clean (no double-bind) instead of deploying
        onto a port another install is about to take.
        """
        import datetime as _dt

        now_iso = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%S")
        # Routed through self.execute() (not self._c.execute) for the WAL
        # busy-retry loop — under checkpoint contention the loser must still
        # resolve cleanly rather than raising OperationalError (#1100 review #1).
        self.execute(
            """INSERT INTO port_reservations (port, key, status, reserved_at)
               VALUES (?, ?, 'reserved', ?)
               ON CONFLICT(port) DO UPDATE SET
                   key = excluded.key,
                   reserved_at = excluded.reserved_at,
                   status = 'reserved'
               WHERE port_reservations.key = excluded.key
                  OR (julianday('now') - julianday(port_reservations.reserved_at)) * 1440
                     >= ?""",
            (port, key, now_iso, PORT_RESERVATION_STALE_MINUTES),
        )
        self._c.commit()
        row = self.execute("SELECT key FROM port_reservations WHERE port = ?", (port,)).fetchone()
        return bool(row) and row[0] == key

    def release_port_reservation(self, port: int) -> None:
        """Remove the port reservation for *port*.

        Called once the new container is confirmed running (or on any failure
        path) so the reservation does not linger and block future installs.
        """
        self._c.execute("DELETE FROM port_reservations WHERE port = ?", (port,))
        self._c.commit()

    # ── Evidence-ranked learning ──────────────────
    # Additive append (keep-both at merge with state.py methods).

    def record_learning_shadow(
        self,
        *,
        app_key: str,
        signature_hash: str,
        image_digest: str,
        learned_score: float,
        legacy_score: float,
        sample_size: int,
        success_count: int,
        failure_count: int,
        digest_match: bool,
        enforced: bool,
    ) -> None:
        """Append one shadow-gate observation to ``learning_shadow_log``.

        Records the derived outcome-weighted score against the legacy flat 0.95
        it replaces so the new scorer can be proven (in shadow) before it is
        allowed to influence behaviour. Never raises on the happy path; callers
        treat learning telemetry as best-effort.
        """
        self._c.execute(
            """
            INSERT INTO learning_shadow_log
                (app_key, signature_hash, image_digest, learned_score, legacy_score,
                 sample_size, success_count, failure_count, digest_match, enforced)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                app_key,
                signature_hash,
                image_digest,
                float(learned_score),
                float(legacy_score),
                int(sample_size),
                int(success_count),
                int(failure_count),
                1 if digest_match else 0,
                1 if enforced else 0,
            ),
        )
        self._c.commit()

    def read_learning_shadow(self, *, limit: int | None = None) -> list[dict[str, Any]]:
        """Return shadow-gate observations (newest first) for the #1088 promotion reporter.

        Read-only counterpart to :meth:`record_learning_shadow`. Returns plain dicts (no
        raw ``sqlite3.Row`` leakage). The promotion *decision* is NOT made here — this is
        the GROUND substrate the observe-only summary (``backend/agent/shadow_promotion``)
        reconciles; the promote-threshold is a separate, review-gated act.
        """
        sql = "SELECT * FROM learning_shadow_log ORDER BY created_at DESC, id DESC"
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        rows = self._c.execute(sql).fetchall()
        return [
            {
                "app_key": r["app_key"],
                "signature_hash": r["signature_hash"],
                "image_digest": r["image_digest"],
                "learned_score": r["learned_score"],
                "legacy_score": r["legacy_score"],
                "sample_size": r["sample_size"],
                "success_count": r["success_count"],
                "failure_count": r["failure_count"],
                "digest_match": bool(r["digest_match"]),
                "enforced": bool(r["enforced"]),
                "created_at": r["created_at"],
            }
            for r in rows
        ]

    def learning_outcome_tally(
        self,
        signature_hash: str,
        *,
        image_digest: str | None = None,
        window_s: int = 7776000,
    ) -> dict[str, int]:
        """Tally fix_history outcomes for *signature_hash* within *window_s*.

        Returns ``{"success": n, "failure": n, "total": n, "digest_match": n}``
        where ``failure`` counts both ``'failed_verification'`` and ``'failure'``
        outcomes (demote-on-failure: a later failure lowers the derived score),
        and ``digest_match`` counts the rows whose ``image_digest`` equals the
        supplied *image_digest* (version-aware reconciliation). Default window is
        90 days. Never raises; an unreachable column yields zeros.
        """
        cutoff = int(time.time()) - window_s
        rows = self._c.execute(
            """
            SELECT outcome, image_digest, COUNT(*) AS n
            FROM fix_history
            WHERE signature_hash = ? AND created_at >= ?
            GROUP BY outcome, image_digest
            """,
            (signature_hash, cutoff),
        ).fetchall()
        tally = {"success": 0, "failure": 0, "total": 0, "digest_match": 0}
        for r in rows:
            outcome = r["outcome"]
            n = int(r["n"])
            tally["total"] += n
            if outcome == "success":
                tally["success"] += n
            elif outcome in ("failed_verification", "failure"):
                tally["failure"] += n
            if image_digest and r["image_digest"] == image_digest:
                tally["digest_match"] += n
        return tally
