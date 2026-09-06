-- Migration 0018: carry the player's position on nba.rankings.
--
-- The rankings API returns name and team but no position, so the rankings
-- player-detail dialog can only show a team where the roster, streamer and
-- matchup dialogs show "TEAM / POS". The other three ranking paths (rolling,
-- categories, league points pool) read tables already joined to nba.players
-- and need no schema change; only the season path reads this view.
--
-- The view already joins nba.players, so this is one more column from a join
-- that is already paid for -- no new scan, no new index.
--
-- nba.players.position is written solely by data-platform's player_profiles
-- pipeline, from the same nba_api PLAYER_INDEX field it writes to
-- nba.player_profiles.position; the two columns never disagree. It is
-- nullable, and a player the profiles pipeline has not seen yet ranks with a
-- NULL position rather than being dropped -- the column is additive.
--
-- Everything below is 0007's definition with `pl.position` appended to the
-- final SELECT. The matview is SELECT * over the view, so its shape follows;
-- rankings_id_key must be recreated for the CONCURRENTLY refresh in
-- data-platform/pipelines/rankings_view.py. Drop the matview before the view
-- it is built from.

DROP MATERIALIZED VIEW IF EXISTS nba.rankings;
DROP VIEW IF EXISTS nba.rankings_source;

CREATE VIEW nba.rankings_source AS
WITH cur AS (
    -- The season being ranked, and the snapshot date it runs through.
    SELECT season, MAX(as_of_date) AS max_as_of
    FROM nba.player_season_stats
    WHERE season = (
        SELECT season
        FROM nba.player_season_stats
        ORDER BY as_of_date DESC
        LIMIT 1
    )
    GROUP BY season
),
latest_per_player AS (
    -- Each player's most recent snapshot within that season.
    SELECT DISTINCT ON (s.player_id)
        s.player_id,
        s.team_id,
        s.gp,
        s.fpts,
        s.as_of_date,
        s.season
    FROM nba.player_season_stats s
    JOIN cur ON cur.season = s.season
    ORDER BY s.player_id, s.as_of_date DESC
),
prior AS (
    -- Each player's most recent snapshot from at least 7 days earlier. Absent
    -- for anyone with no history that far back; see the COALESCE below.
    SELECT DISTINCT ON (s.player_id)
        s.player_id,
        s.fpts
    FROM nba.player_season_stats s
    JOIN cur ON cur.season = s.season
    WHERE s.as_of_date <= (cur.max_as_of - INTERVAL '7 days')::date
    ORDER BY s.player_id, s.as_of_date DESC
),
ranked AS (
    -- League-wide ranks now and a week ago. A player with no prior snapshot
    -- keeps his current fpts on both sides, so his rank_change comes out ~0
    -- rather than a fabricated surge.
    SELECT
        l.player_id,
        l.team_id,
        l.gp,
        l.fpts,
        l.as_of_date,
        l.season,
        RANK() OVER (ORDER BY l.fpts DESC)                                     AS curr_rank,
        RANK() OVER (ORDER BY COALESCE(p.fpts, l.fpts) DESC)                   AS prev_rank
    FROM latest_per_player l
    LEFT JOIN prior p ON p.player_id = l.player_id
)
SELECT
    pl.id,
    r.curr_rank,
    pl.name,
    r.team_id                                                                  AS team,
    r.fpts,
    ROUND(1.0 * r.fpts::numeric / NULLIF(r.gp, 0)::numeric, 2)                 AS avg_fpts,
    r.prev_rank - r.curr_rank                                                  AS rank_change,
    r.gp,
    r.as_of_date,
    r.season,
    pl.position
FROM ranked r
JOIN nba.players pl ON r.player_id = pl.id
ORDER BY r.curr_rank;

CREATE MATERIALIZED VIEW nba.rankings AS
SELECT * FROM nba.rankings_source
WITH DATA;

CREATE UNIQUE INDEX rankings_id_key ON nba.rankings (id);

COMMENT ON MATERIALIZED VIEW nba.rankings IS
  'Materialized nba.rankings_source. Refreshed by the data-platform post-game player_season_stats pipeline; the API falls back to nba.rankings_source when this copy lags nba.player_season_stats. rank_change is versus 7 days ago.';
