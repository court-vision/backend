-- Migration 0059: Dime's chat in a draft room.
--
-- James (2026-11-26), step 2 of the Draft lab's Dime rework: each room gets a
-- Dime chat beside its board, where the user can ask anything about the draft
-- (a plan included), and each player on the board gets an Ask Dime button
-- that puts a question in its box. The chat is the AI desk's agent with the
-- AI desk's tools, one conversation per room, with the room's state on every
-- turn. It is a conversation of its own kind, `room`, so the AI desk doesn't
-- list it among the user's chats, and `usr.ai_draft_rooms.chat_id` says which
-- conversation is the room's. A deleted conversation leaves the room with no
-- chat (the next message starts a new one).

ALTER TABLE usr.ai_conversations DROP CONSTRAINT IF EXISTS ai_conversations_kind_check;
ALTER TABLE usr.ai_conversations ADD CONSTRAINT ai_conversations_kind_check
    CHECK (kind IN ('chat', 'checkin', 'draft', 'room'));

ALTER TABLE usr.ai_draft_rooms ADD COLUMN IF NOT EXISTS chat_id bigint
    REFERENCES usr.ai_conversations (id) ON DELETE SET NULL;

COMMENT ON COLUMN usr.ai_draft_rooms.chat_id IS
    'The room''s Dime chat (a conversation of kind room), when the user has started one';
