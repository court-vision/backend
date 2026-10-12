-- Rollback 0031. The status repair is not undone: those games are finished.
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
