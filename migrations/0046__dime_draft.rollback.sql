DROP TABLE IF EXISTS usr.ai_draft_notes;
DROP TABLE IF EXISTS usr.ai_draft_rooms;
-- Dime's picks become hand-entered ones (they were the user's seat's picks all along)
UPDATE usr.draft_picks SET source = 'manual' WHERE source = 'dime';
ALTER TABLE usr.draft_picks DROP CONSTRAINT IF EXISTS draft_picks_source_check;
ALTER TABLE usr.draft_picks ADD CONSTRAINT draft_picks_source_check
    CHECK (source IN ('manual', 'espn_sync', 'import', 'keeper', 'mock'));
DELETE FROM usr.ai_conversations WHERE kind = 'draft';
ALTER TABLE usr.ai_conversations DROP CONSTRAINT IF EXISTS ai_conversations_kind_check;
ALTER TABLE usr.ai_conversations ADD CONSTRAINT ai_conversations_kind_check
    CHECK (kind IN ('chat', 'checkin'));
