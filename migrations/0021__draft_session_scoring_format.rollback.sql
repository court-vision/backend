ALTER TABLE usr.draft_sessions DROP CONSTRAINT IF EXISTS draft_sessions_scoring_format_check;
ALTER TABLE usr.draft_sessions DROP COLUMN IF EXISTS scoring_format;
