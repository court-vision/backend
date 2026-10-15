-- The rollback keeps the audit history: reactions written under source
-- 'availability' stay in usr.roster_moves. The three-value source CHECK comes
-- back NOT VALID, so Postgres skips those rows and checks every new or updated
-- one; applying 0034 again re-adds (and validates) the four-value one. `source`
-- stays varchar(16): narrowing it would fail on those rows, and the width is
-- harmless to the previous code. trigger_player_id goes with its index.
DROP INDEX IF EXISTS usr.roster_moves_availability_once_uq;
ALTER TABLE usr.roster_moves DROP COLUMN IF EXISTS trigger_player_id;
ALTER TABLE usr.roster_moves DROP CONSTRAINT IF EXISTS roster_moves_source_check;
ALTER TABLE usr.roster_moves ADD CONSTRAINT roster_moves_source_check
    CHECK (source IN ('manual', 'auto', 'scheduled')) NOT VALID;

DROP TABLE IF EXISTS nba.player_availability;
DROP TABLE IF EXISTS nba.availability_reports;
DROP TABLE IF EXISTS nba.news_item_teams;
DROP TABLE IF EXISTS nba.news_item_players;
DROP TABLE IF EXISTS nba.news_items;
