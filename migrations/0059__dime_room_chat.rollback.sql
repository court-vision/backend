-- Rollback 0059: the rooms' Dime chats go, with their conversations and turns.
ALTER TABLE usr.ai_draft_rooms DROP COLUMN IF EXISTS chat_id;
DELETE FROM usr.ai_conversations WHERE kind = 'room';
ALTER TABLE usr.ai_conversations DROP CONSTRAINT IF EXISTS ai_conversations_kind_check;
ALTER TABLE usr.ai_conversations ADD CONSTRAINT ai_conversations_kind_check
    CHECK (kind IN ('chat', 'checkin', 'draft'));
