-- Migration 009: router_decisions — persist LLM routing outcomes
--
-- Records every routing decision made by the router engine so that cost,
-- latency, and provider-selection patterns can be analysed over time.
--
-- Fields:
--   prompt_chars     — length of the prompt (chars) for cost estimation
--   tier             — Tier name string (SIMPLE | STANDARD | COMPLEX | REASONING)
--   chain            — JSON array of provider names in dispatch order
--   chosen_provider  — provider that ultimately responded successfully (NULL if all failed)
--   outcome          — 'success' | 'all_failed' | NULL (dry-run / not dispatched yet)
--   cost_usd         — estimated cost in USD (NULL if unknown or local provider)
--   latency_ms       — wall-clock dispatch latency in milliseconds (NULL if not measured)
--   created_at       — Unix timestamp (seconds since epoch)

BEGIN;

CREATE TABLE IF NOT EXISTS router_decisions (
    id               INTEGER PRIMARY KEY,
    prompt_chars     INT,
    tier             TEXT,
    chain            TEXT,          -- JSON array, e.g. '["ollama","openai"]'
    chosen_provider  TEXT,
    outcome          TEXT,          -- 'success' | 'all_failed' | NULL
    cost_usd         REAL,
    latency_ms       INT,
    created_at       INT DEFAULT (unixepoch())
);

CREATE INDEX IF NOT EXISTS idx_router_decisions_created_at
    ON router_decisions (created_at);

CREATE INDEX IF NOT EXISTS idx_router_decisions_chosen_provider
    ON router_decisions (chosen_provider);

COMMIT;
