-- Rollback 0056: the whole-app cap goes; its row, if one was set, goes with it.
DELETE FROM usr.ai_caps WHERE line = 'app';

ALTER TABLE usr.ai_caps DROP CONSTRAINT IF EXISTS ai_caps_user_check;
ALTER TABLE usr.ai_caps
    ADD CONSTRAINT ai_caps_user_check CHECK (user_id IS NULL OR line <> 'analyst');

ALTER TABLE usr.ai_caps DROP CONSTRAINT IF EXISTS ai_caps_line_check;
ALTER TABLE usr.ai_caps
    ADD CONSTRAINT ai_caps_line_check CHECK (line IN ('desk', 'autopilot', 'analyst'));
