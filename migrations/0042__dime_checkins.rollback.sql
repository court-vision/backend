DROP INDEX IF EXISTS usr.ai_spend_line_created;
ALTER TABLE usr.ai_spend DROP CONSTRAINT IF EXISTS ai_spend_line_check;
ALTER TABLE usr.ai_spend DROP COLUMN IF EXISTS line;
DELETE FROM usr.ai_proposals WHERE state = 'shadow';
ALTER TABLE usr.ai_proposals DROP CONSTRAINT IF EXISTS ai_proposals_state_check;
ALTER TABLE usr.ai_proposals ADD CONSTRAINT ai_proposals_state_check
    CHECK (state IN ('proposed', 'sent', 'dismissed'));
DROP TABLE IF EXISTS usr.ai_decisions;
DROP TABLE IF EXISTS usr.ai_checkins;
DELETE FROM usr.ai_conversations WHERE kind = 'checkin';
ALTER TABLE usr.ai_conversations DROP CONSTRAINT IF EXISTS ai_conversations_kind_check;
ALTER TABLE usr.ai_conversations DROP COLUMN IF EXISTS kind;
DROP TABLE IF EXISTS usr.autopilot;
