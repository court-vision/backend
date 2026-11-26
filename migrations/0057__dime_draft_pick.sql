-- Migration 0057: Dime's pick in a draft room.
--
-- James (2026-11-26): the Draft lab drops Dime's plan and its pick notes. In their
-- place the board highlights the player Dime would take at the user's next pick,
-- with one line on why for their roster. A pick note (kind 'pick', not the seat's)
-- now records that choice here through the recommend_pick tool: the player and
-- the reason, as {"player_id", "name", "position", "team", "reason"}. NULL on
-- every older note and on the seat's notes, whose pick is `took`.

ALTER TABLE usr.ai_draft_notes ADD COLUMN IF NOT EXISTS pick jsonb;

COMMENT ON COLUMN usr.ai_draft_notes.pick IS
    'A pick note''s choice for the user''s next pick (recommend_pick): player_id, name, position, team, reason';
