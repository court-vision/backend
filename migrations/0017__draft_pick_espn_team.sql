-- Migration 0017: which ESPN team made each pick.
--
-- A pick's seat was derived from its number and the snake geometry. ESPN says
-- who actually picked on every SELECTED frame and every INIT pick, and that is
-- what a traded pick or an auction needs. Stored as the ESPN team id — the
-- same ids `pick_order` holds — so a seat becomes a lookup, not a guess.

ALTER TABLE usr.draft_picks ADD COLUMN IF NOT EXISTS espn_team_id bigint;
