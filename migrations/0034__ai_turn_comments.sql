-- Migration 0034: a free-text comment on an AI agent turn (docs/AI_AGENT_PLAN.md).
--
-- The invited users' notes on an answer -- what was wrong, what they expected --
-- left in the AI desk beside 👍 / 👎 and read back when the answers are reviewed.

ALTER TABLE usr.ai_turns
    ADD COLUMN IF NOT EXISTS comment text,
    ADD COLUMN IF NOT EXISTS comment_at timestamp with time zone;
