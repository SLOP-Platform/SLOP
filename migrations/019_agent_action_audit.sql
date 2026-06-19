-- migration 019: agent_action_audit table
--
-- Append-only audit trail for every autonomous agent action.
-- Rows are NEVER updated or deleted; each invocation writes a QUEUED row first
-- (intent captured before handler fires) then an OUTCOME row on completion.
-- The run_id ties the two rows together.
--
-- This table is the "what did you do" source for the chat panel (W6/N6) and
-- the audit actor defined in backend/agent/audit.py.
--
-- No DELETE or UPDATE trigger is defined here: append-only is structural, not
-- just conventional.  The application module (audit.py) provides no UPDATE path.

CREATE TABLE IF NOT EXISTS agent_action_audit (
    id          INTEGER  PRIMARY KEY AUTOINCREMENT,
    run_id      TEXT     NOT NULL,
    ts          INTEGER  NOT NULL,
    trigger     TEXT     NOT NULL CHECK(trigger IN ('scheduler','chat','api','unknown')),
    action_id   TEXT     NOT NULL,
    app_key     TEXT     NOT NULL,
    tier        INTEGER  NOT NULL CHECK(tier BETWEEN 0 AND 3),
    status      TEXT     NOT NULL CHECK(status IN ('queued','ok','failed','rolled_back')),
    outcome_msg TEXT,
    rollback    INTEGER  NOT NULL DEFAULT 0 CHECK(rollback IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_agent_audit_run_id  ON agent_action_audit (run_id);
CREATE INDEX IF NOT EXISTS idx_agent_audit_ts      ON agent_action_audit (ts DESC);
CREATE INDEX IF NOT EXISTS idx_agent_audit_app_key ON agent_action_audit (app_key, ts DESC);
