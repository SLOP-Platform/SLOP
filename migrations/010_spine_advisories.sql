-- Migration 010: spine_advisories — stores LLM advisory annotations.
-- Advisory records are STORE-ONLY; no automated remediation is triggered from this table.
CREATE TABLE IF NOT EXISTS spine_advisories (
    id          INTEGER PRIMARY KEY,
    finding_id  TEXT    NOT NULL,
    verdict     TEXT    NOT NULL,
    annotation  TEXT    NOT NULL,  -- JSON
    provider    TEXT    NOT NULL,
    created_at  INTEGER NOT NULL DEFAULT (unixepoch())
);
