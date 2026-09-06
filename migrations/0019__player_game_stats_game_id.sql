-- Migration 0019: identify a box-score row by the game it belongs to.
--
-- `nba.player_game_stats` has recorded what a player did on a date, never which
-- game he did it in, so every reader that wanted the fixture inferred it from
-- (game_date, team) against nba.games. That inference is correct today only
-- because an NBA team plays at most once a day — a fact about the sport, not a
-- guarantee of the schema, and one the code had to restate everywhere it
-- resolved an opponent.
--
-- The id is not new data. nba_api's PlayerGameLogs payload carries GAME_ID, and
-- the ingest pipeline already reads that column for its own gate; it simply
-- never stored it. The backfill below reconstructs it for existing rows from
-- the same join the readers were doing, run once instead of per request.
--
-- Measured before writing this, on a database cloned from production: all
-- 26,422 rows resolve to exactly one game, and none is ambiguous. The column is
-- still nullable, because a migration that fails on a row this has not seen
-- would take production down with it, and because a row whose game is genuinely
-- missing should be storable.

ALTER TABLE nba.player_game_stats
    ADD COLUMN IF NOT EXISTS game_id varchar(20)
        REFERENCES nba.games(game_id) ON DELETE SET NULL;

-- Backfill. (game_date, team) is unique in practice; the GROUP BY/HAVING guard
-- means a date that somehow offers two candidates is left NULL rather than
-- assigned one of them at random.
UPDATE nba.player_game_stats pgs
SET game_id = m.game_id
FROM (
    SELECT s.id AS stats_id, MIN(g.game_id) AS game_id
    FROM nba.player_game_stats s
    JOIN nba.games g
      ON g.game_date = s.game_date
     AND (g.home_team_id = s.team_id OR g.away_team_id = s.team_id)
    WHERE s.team_id IS NOT NULL AND s.game_id IS NULL
    GROUP BY s.id
    HAVING count(*) = 1
) m
WHERE pgs.id = m.stats_id;

CREATE INDEX IF NOT EXISTS player_game_stats_game_id_idx
    ON nba.player_game_stats (game_id);

-- The row's identity moves from the date to the game.
--
-- The date index is kept as a *partial* unique index rather than dropped
-- outright: a NULL game_id is distinct from every other NULL in Postgres, so
-- swapping the constraint outright would leave any unresolved row with no
-- uniqueness protection at all — the pipeline could then write it twice. The
-- two indexes cover disjoint sets of rows, so together they are exactly the old
-- guarantee plus a sharper one, and the partial index disappears on its own
-- once every row has a game.
-- Peewee named it, so it is an index rather than a table constraint.
DROP INDEX IF EXISTS nba.playergamestats_player_id_game_date;

CREATE UNIQUE INDEX IF NOT EXISTS player_game_stats_player_game_uq
    ON nba.player_game_stats (player_id, game_id);

CREATE UNIQUE INDEX IF NOT EXISTS player_game_stats_player_date_unresolved_uq
    ON nba.player_game_stats (player_id, game_date)
    WHERE game_id IS NULL;

COMMENT ON COLUMN nba.player_game_stats.game_id IS
  'The game this line belongs to. NULL only when nba.games has no matching fixture; readers fall back to a (game_date, team) lookup for those rows.';
