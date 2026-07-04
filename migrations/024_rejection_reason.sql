-- Migration 024: rejection_reason column on fix_history (#1164 rejection-learning)
-- Wire reject_fix to optionally capture a free-text rejection reason and
-- feed it into the LLM diagnosis context so the agent can learn from user
-- feedback.  Outcome vocabulary is NOT changed: rejections stay
-- outcome='failure' to preserve the learning_outcome_tally contract
-- (tested in test_fix_outcome_contract.py).
ALTER TABLE fix_history ADD COLUMN rejection_reason TEXT NOT NULL DEFAULT '';
