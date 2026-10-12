-- Rollback 0031. The status repair is not undone: those games are finished.
-- The tov_pct scale is, so that re-applying 0031 can't divide it twice
-- (precision is lost: numeric(6,3) kept three decimals of the fraction).
UPDATE nba.player_advanced_stats
SET tov_pct = tov_pct * 100
WHERE tov_pct IS NOT NULL;

DROP INDEX IF EXISTS nba.player_game_stats_season_type_idx;

ALTER TABLE nba.player_game_stats
    DROP COLUMN IF EXISTS plus_minus,
    DROP COLUMN IF EXISTS pf,
    DROP COLUMN IF EXISTS dreb,
    DROP COLUMN IF EXISTS oreb,
    DROP COLUMN IF EXISTS season_type,
    DROP COLUMN IF EXISTS season;

ALTER TABLE nba.games
    DROP COLUMN IF EXISTS season_type;
