"""backend/api/jobs_store.py

SQLite-authoritative job state with in-memory write-through cache.

Authority model (updated)
--------------------------
SQLite is now authoritative for job state; the in-memory dict is a write-through
cache for hot-path reads. This means:

- ``set_job``: writes to DB first, then updates cache.
- ``get_job``: returns from cache; if missing, falls back to DB.
- On module init: loads all ``status='running'`` jobs from DB, marks them
  ``'unknown'``, and populates the cache.  (``StateDB.mark_running_jobs_unknown()``
  is also called at lifespan startup in ``state.init_db()``; this module-level
  sweep ensures the cache reflects that state immediately.)

A job whose DB status is ``'unknown'`` returns:
  ``{"status": "unknown", "message": "Server restarted during job — check manually."}``
from ``get_job``.

Background
----------
The platform wizard ("run-async") and the Ollama setup flow each spawn a
background thread and hand the frontend a ``job_id`` to poll.  Pre-refactor,
job state lived only in process-memory dicts (``_wizard_jobs`` / ``_ollama_jobs``
in ``backend.api.platform``), so any backend restart orphaned polling clients:
they kept polling a ``job_id`` that no longer existed in memory.

The ``persist_job`` / ``load_wizard_job`` / ``load_ollama_job`` helpers are kept
for the existing platform.py callers; new code should use ``set_job`` / ``get_job``.

Restart semantics: a job left ``status='running'`` when the process restarts is
flipped to ``'unknown'`` by ``StateDB.mark_running_jobs_unknown()`` at startup.
The loaders below surface such a job as ``done=True`` so the polling client stops
spinning and can offer a retry — never auto-restarted, never marked failed.
"""

from __future__ import annotations

import threading
from typing import Any

from backend.core.state import StateDB

# ── In-memory write-through cache ────────────────────────────────────────────
# SQLite is authoritative; this dict is the hot-path read layer.
# Populated at module init from any orphaned-running jobs in DB, then kept
# in sync by set_job()/get_job().
_job_cache: dict[str, dict[str, Any]] = {}
_job_cache_lock = threading.Lock()


def _init_cache_from_db() -> None:
    """Load orphaned running jobs from DB into cache as 'unknown'.

    Called once at module import.  Safe to call again (idempotent — only adds
    entries that are not already in cache).  Failures are swallowed so an
    unavailable DB at import time does not prevent the module from loading.
    """
    try:
        with StateDB() as db:
            rows = db.execute(
                "SELECT id, kind, status, payload FROM jobs WHERE status IN ('running', 'unknown')"
            ).fetchall()
        with _job_cache_lock:
            for row in rows:
                job_id = row["id"]
                if job_id in _job_cache:
                    continue  # already populated; don't overwrite live entry
                import json as _j

                payload: dict[str, Any] = {}
                try:
                    payload = _j.loads(row["payload"]) if row["payload"] else {}
                except (ValueError, TypeError):
                    pass
                if row["status"] == "running":
                    # This process did not finish the job — mark unknown in cache
                    payload["status"] = "unknown"
                    payload["message"] = "Server restarted during job — check manually."
                _job_cache[job_id] = {"kind": row["kind"], "status": row["status"], **payload}
    except Exception:  # noqa: S110  # best-effort; DB may not be configured yet at import
        pass


# Populate cache once at import time.
_init_cache_from_db()


def set_job(key: str, job_data: dict[str, Any]) -> None:
    """Write job_data to DB first (authoritative), then update in-memory cache.

    ``job_data`` must contain a ``"kind"`` key (``"wizard"`` or ``"ollama"``) so
    that the DB row can be created with the correct kind column.
    """
    kind = job_data.get("kind", "wizard")
    status = derive_job_status(kind, job_data)
    try:
        with StateDB() as db:
            db.create_job(key, kind, status, job_data)
    except Exception:  # noqa: S110  # DB write failure must not break the running job thread
        pass
    with _job_cache_lock:
        _job_cache[key] = dict(job_data)


def get_job(key: str) -> dict[str, Any] | None:
    """Return job dict from cache; fall back to DB on a cache miss.

    A job whose DB status is ``'unknown'`` (orphaned across a restart) returns a
    sentinel payload:
      ``{"status": "unknown", "message": "Server restarted during job — check manually."}``
    Returns None if no job exists with the given key.
    """
    with _job_cache_lock:
        cached = _job_cache.get(key)
    if cached is not None:
        if cached.get("status") == "unknown":
            return {
                "status": "unknown",
                "message": "Server restarted during job — check manually.",
            }
        return dict(cached)

    # Cache miss — try DB
    try:
        with StateDB() as db:
            row = db.get_job(key)
    except Exception:
        return None
    if row is None:
        return None

    payload = dict(row["payload"])
    if row["status"] == "unknown":
        # Populate cache with unknown sentinel, return sentinel to caller
        with _job_cache_lock:
            _job_cache[key] = {"kind": row["kind"], "status": "unknown"}
        return {
            "status": "unknown",
            "message": "Server restarted during job — check manually.",
        }

    with _job_cache_lock:
        _job_cache[key] = {"kind": row["kind"], "status": row["status"], **payload}
    return payload


def derive_job_status(kind: str, job: dict[str, Any]) -> str:
    """Map an in-memory job dict to a ``jobs.status`` value.

    'error' if the job reports an error, 'done' once complete, else 'running'.
    """
    if kind == "ollama":
        if job.get("phase") == "error" or job.get("errorDetail"):
            return "error"
        return "done" if job.get("done") else "running"
    # wizard
    if job.get("error"):
        return "error"
    return "done" if job.get("done") else "running"


def persist_job(job_id: str, kind: str, job: dict[str, Any]) -> None:
    """Mirror an in-memory job dict to the SQLite ``jobs`` table (best-effort).

    Persistence never breaks the running flow — a DB hiccup must not crash the
    wizard/Ollama thread, so failures are swallowed (the in-memory dict is still
    authoritative for the live client). The derived ``status`` column lets the
    startup sweep flip orphaned 'running' jobs to 'unknown' after a restart.
    """
    try:
        status = derive_job_status(kind, job)
        with StateDB() as db:
            db.create_job(job_id, kind, status, dict(job))
    except Exception:  # noqa: S110  # persistence is best-effort, never fatal; in-memory dict is authoritative
        pass


def load_wizard_job(job_id: str) -> dict[str, Any] | None:
    """Reconstruct an in-memory-shaped wizard job dict from the jobs table.

    Returns None if there is no such (wizard) job. A row whose status the
    startup sweep flipped to 'unknown' is surfaced as ``done=True`` so polling
    clients stop spinning; the payload's own fields are otherwise preserved.
    """
    try:
        with StateDB() as db:
            row = db.get_job(job_id)
    except Exception:  # DB read failure should not 500 the poll
        return None
    if row is None or row["kind"] != "wizard":
        return None
    payload = dict(row["payload"])
    payload.setdefault("steps", [])
    payload.setdefault("platform_ready", False)
    payload.setdefault("error", None)
    payload.setdefault("started_at", float(row["created_at"]))
    # An orphaned (unknown) job can no longer make progress — present it as done
    # so the client stops polling, but never as platform_ready / error.
    payload["done"] = bool(payload.get("done")) or row["status"] == "unknown"
    return payload


def load_ollama_job(job_id: str) -> dict[str, Any] | None:
    """Reconstruct an Ollama job dict from the jobs table (None if absent).

    An orphaned (status='unknown') job is surfaced as ``phase='unknown'``,
    ``done=True`` so the polling client stops spinning and can offer a retry.
    """
    try:
        with StateDB() as db:
            row = db.get_job(job_id)
    except Exception:  # DB read failure should not 500 the poll
        return None
    if row is None or row["kind"] != "ollama":
        return None
    payload = dict(row["payload"])
    if row["status"] == "unknown":
        payload["phase"] = "unknown"
        payload["done"] = True
    return payload
