-- Migration 0036: the two views behind the agent's query_stats tool
-- (docs/QUERY_STATS_PLAN.md).
--
-- query_stats compiles a small spec the model writes into SQL over these two
-- views and nothing else, and runs it as the read-only role ask_reader
-- (ops/roles.sql). The views are the only code that knows the tables: a
-- column that moves lands here, not in the compiler or the metric catalog
-- (services/ai/stats/catalog.py).
--
-- nba.ask_player_games: one row per box-score line, with its game and player.
--   - `played` is min > 0: a sub-minute appearance is a row every fantasy
--     reader skips, and a rolling mean must not count it as a game.
--   - `game_no` numbers a player's *played* games within a season and season
--     type in date order (NULL on a game he did not play), so two players'
--     seasons line up on a chart by game rather than by date.
--   - The NBA Cup final counts as a regular-season game, as every fantasy
--     reader counts it (FANTASY_SEASON_TYPES in db/models/nba/games.py), so
--     season_type is 'regular', 'playoffs' or 'play_in'. Preseason and
--     All-Star lines are not stored (0032).
--   - opponent, home and won come from nba.games through the row's game_id;
--     a line whose game is unknown keeps its box score with those three NULL,
--     and a line with no season at all (no game id) is left out.
--
-- nba.ask_player_seasons: one row per player per regular season of totals.
--   - nba.player_history for completed seasons (the season_history pipeline
--     writes a season once it is over).
--   - For a season history does not have yet (the current one), the latest
--     nba.player_season_stats snapshot per player: snapshots are sparse, so
--     "latest" is per player and season, never one date for everyone. Once
--     the yearly history append lands, that season's rows come from history
--     and the snapshot rows drop out of the view by themselves.
--   - fpts on history rows is cv_core's formula over the totals. Its weights
--     are integers, so this equals the sum of the per-game fpts exactly; the
--     integration test pins the expression against calculate_fantasy_points.
--   - Rates are not stored: a reader derives FG% from fgm and fga, never from
--     averaged percentages.
--
-- Both are plain views. A window over one player-season is milliseconds, and
-- ask_player_games partitions its window by player_id, season and season_type,
-- so a filter on those columns is pushed below the window function and a
-- per-player query never numbers the whole table.
--
-- A later revision recreates a view with DROP VIEW + CREATE VIEW and must
-- repeat the grants below: dropping a view drops them.

DROP VIEW IF EXISTS nba.ask_player_games;

CREATE VIEW nba.ask_player_games AS
WITH lines AS (
    SELECT
        s.player_id,
        s.team_id,
        s.game_id,
        s.game_date,
        COALESCE(s.season, g.season) AS season,
        CASE COALESCE(s.season_type, g.season_type)
            WHEN 'cup_final' THEN 'regular'
            ELSE COALESCE(s.season_type, g.season_type)
        END AS season_type,
        g.home_team_id,
        g.away_team_id,
        g.home_score,
        g.away_score,
        g.status,
        s.min, s.pts, s.reb, s.ast, s.stl, s.blk, s.tov,
        s.fgm, s.fga, s.fg3m, s.fg3a, s.ftm, s.fta,
        s.oreb, s.dreb, s.pf, s.plus_minus,
        s.fpts
    FROM nba.player_game_stats s
    LEFT JOIN nba.games g ON g.game_id = s.game_id
)
SELECT
    l.player_id,
    p.name,
    l.team_id AS team,
    l.season,
    l.season_type,
    l.game_date AS date,
    l.game_id,
    CASE
        WHEN l.home_team_id = l.team_id THEN l.away_team_id
        WHEN l.away_team_id = l.team_id THEN l.home_team_id
    END AS opponent,
    CASE
        WHEN l.home_team_id = l.team_id THEN TRUE
        WHEN l.away_team_id = l.team_id THEN FALSE
    END AS home,
    CASE
        WHEN l.status IS DISTINCT FROM 'final'
          OR l.home_score IS NULL OR l.away_score IS NULL
          OR l.home_score = l.away_score THEN NULL
        WHEN l.home_team_id = l.team_id THEN l.home_score > l.away_score
        WHEN l.away_team_id = l.team_id THEN l.away_score > l.home_score
    END AS won,
    l.min > 0 AS played,
    CASE WHEN l.min > 0 THEN
        ROW_NUMBER() OVER (
            PARTITION BY l.player_id, l.season, l.season_type, l.min > 0
            ORDER BY l.game_date, l.game_id
        )
    END AS game_no,
    l.min, l.pts, l.reb, l.ast, l.stl, l.blk, l.tov,
    l.fgm, l.fga, l.fg3m, l.fg3a, l.ftm, l.fta,
    l.oreb, l.dreb, l.pf, l.plus_minus,
    l.fpts
FROM lines l
JOIN nba.players p ON p.id = l.player_id
WHERE l.season IS NOT NULL
  AND l.season_type IN ('regular', 'playoffs', 'play_in');

COMMENT ON VIEW nba.ask_player_games IS
  'One row per box-score line with its game, for the agent''s query_stats tool (docs/QUERY_STATS_PLAN.md). played = min > 0; game_no counts played games per player, season and season type in date order; the Cup final is a regular-season game here.';

DROP VIEW IF EXISTS nba.ask_player_seasons;

CREATE VIEW nba.ask_player_seasons AS
SELECT
    h.player_id,
    COALESCE(p.name, h.player_name)::varchar(100) AS name,
    h.season,
    h.team::varchar(5) AS team,
    'history'::text AS source,
    h.gp::integer AS gp,
    h.min::numeric AS min,
    h.pts::integer AS pts,
    h.reb::integer AS reb,
    h.ast::integer AS ast,
    h.stl::integer AS stl,
    h.blk::integer AS blk,
    h.tov::integer AS tov,
    h.fgm::integer AS fgm,
    h.fga::integer AS fga,
    h.fg3m::integer AS fg3m,
    h.fg3a::integer AS fg3a,
    h.ftm::integer AS ftm,
    h.fta::integer AS fta,
    h.oreb::integer AS oreb,
    h.dreb::integer AS dreb,
    -- cv_core.scoring_vocab.DEFAULT_POINT_WEIGHTS over the totals.
    (h.pts::integer + h.reb + 2 * h.ast + 4 * (h.stl::integer + h.blk) - 2 * h.tov
     + h.fg3m + 2 * h.fgm - h.fga + h.ftm - h.fta)::integer AS fpts,
    h.dd2::integer AS dd2,
    h.td3::integer AS td3,
    h.usg_pct::numeric AS usg_pct
FROM nba.player_history h
LEFT JOIN nba.players p ON p.id = h.player_id
UNION ALL
SELECT
    s.player_id,
    p.name::varchar(100),
    s.season,
    s.team_id::varchar(5),
    'snapshot'::text,
    s.gp::integer,
    s.min::numeric,
    s.pts::integer, s.reb::integer, s.ast::integer, s.stl::integer, s.blk::integer, s.tov::integer,
    s.fgm::integer, s.fga::integer, s.fg3m::integer, s.fg3a::integer, s.ftm::integer, s.fta::integer,
    NULL::integer, NULL::integer,
    s.fpts::integer,
    NULL::integer, NULL::integer, NULL::numeric
FROM nba.player_season_stats s
JOIN nba.players p ON p.id = s.player_id
WHERE s.as_of_date = (
        SELECT MAX(s2.as_of_date)
        FROM nba.player_season_stats s2
        WHERE s2.player_id = s.player_id AND s2.season = s.season
      )
  AND NOT EXISTS (
        SELECT 1 FROM nba.player_history h
        WHERE h.player_id = s.player_id AND h.season = s.season
      );

COMMENT ON VIEW nba.ask_player_seasons IS
  'One row per player per regular season of totals, for the agent''s query_stats tool: nba.player_history for completed seasons, the latest nba.player_season_stats snapshot per player for a season history lacks. source says which. Rates are derived from makes and attempts by the reader.';

-- ask_reader (ops/roles.sql) reads these two views and nothing else. A fresh
-- or test database has no such role and must still migrate, hence the guard.
-- Views run with their owner's privileges, so the role never needs, and never
-- gets, SELECT on the tables underneath.
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ask_reader') THEN
        GRANT USAGE ON SCHEMA nba TO ask_reader;
        GRANT SELECT ON nba.ask_player_games, nba.ask_player_seasons TO ask_reader;
    END IF;
END
$$;
