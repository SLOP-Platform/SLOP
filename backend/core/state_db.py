# Thread-local SQLite connection helpers, extracted from state.py
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

_thread_local: threading.local = threading.local()


def _get_thread_conn(path: Path) -> sqlite3.Connection:
    """Return the cached per-thread connection, opening if necessary or if path changed."""
    conn: sqlite3.Connection | None = getattr(_thread_local, "conn", None)
    conn_path: Path | None = getattr(_thread_local, "conn_path", None)
    if conn is not None and conn_path != path:
        # Path changed on this thread (e.g. test fixture rotated the DB path);
        # close the stale connection so we open a fresh one against the new file.
        try:
            conn.close()
        except Exception:  # noqa: S110  # best-effort close of stale connection; open fresh below
            pass
        conn = None
    if conn is None:
        conn = sqlite3.connect(str(path), timeout=10, check_same_thread=True)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        # WAL (Write-Ahead Logging) mode: readers never block writers and writers
        # never block readers, so the health-cycle, batch-install thread, and API
        # handlers can all proceed concurrently with very low contention.
        # check_same_thread=True above is intentional: each thread opens its own
        # connection (thread-local storage), so SQLite's per-thread serialization is
        # correct.  StateDB.execute() adds an application-level retry loop for the
        # rare "database is locked" / "database is busy" errors that can still occur
        # during WAL checkpointing (id=471 — confirmed enabled, verified by test).
        conn.execute("PRAGMA journal_mode = WAL")
        # NORMAL sync is safe under WAL: data is durable across crashes, with
        # only OS-crash (power-loss) durability traded off (acceptable for SLOP).
        conn.execute("PRAGMA synchronous = NORMAL")
        _thread_local.conn = conn
        _thread_local.conn_path = path
    return conn


def _close_thread_conn() -> None:
    """Close and clear the current thread's cached connection, if any."""
    conn: sqlite3.Connection | None = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:  # noqa: S110  # best-effort close of thread-local connection on teardown
            pass
        _thread_local.conn = None
        _thread_local.conn_path = None
