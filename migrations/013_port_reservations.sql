-- Migration 012: port_reservations table for TOCTOU race prevention in replace_app.
--
-- When replace_app stops an old container to free a port for a new one, there is
-- a window where another process could claim that port.  This table records a
-- DB-level reservation before any container operation begins; _check_port_conflict
-- blocks concurrent callers that try to claim a reserved port.
--
-- Lifecycle: reserved → (new container confirmed running) → deleted.
-- Reservations that linger past a crash are expired by the TTL check in
-- _check_port_conflict (> 5 minutes old = stale, ignored).

CREATE TABLE IF NOT EXISTS port_reservations (
    port        INTEGER PRIMARY KEY,
    key         TEXT    NOT NULL,   -- app key that holds the reservation
    status      TEXT    NOT NULL DEFAULT 'reserved',
    reserved_at TEXT    NOT NULL    -- ISO-8601 UTC timestamp
);
