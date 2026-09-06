-- Rollback 0019: back to identifying a row by its date.
--
-- Restores the original unique constraint before dropping the column, so the
-- table is never briefly unprotected.

CREATE UNIQUE INDEX IF NOT EXISTS playergamestats_player_id_game_date
    ON nba.player_game_stats (player_id, game_date);

DROP INDEX IF EXISTS nba.player_game_stats_player_game_uq;
DROP INDEX IF EXISTS nba.player_game_stats_player_date_unresolved_uq;
DROP INDEX IF EXISTS nba.player_game_stats_game_id_idx;

ALTER TABLE nba.player_game_stats DROP COLUMN IF EXISTS game_id;
