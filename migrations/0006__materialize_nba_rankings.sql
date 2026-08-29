-- Migration 0006: materialize nba.rankings, and carry gp/as_of_date/season on it.
--
-- The query behind the 0002 view sorts every season snapshot (18,527 rows in
-- August 2026, ~59% of a season) and runs a LEAD() window over them before
-- DISTINCT ON reduces it to one row per player, then two more sort+RANK()
-- passes. Cost scales with players x snapshot days, so it grows all season.
-- Every public rankings request paid it.
--
-- Two objects now:
--
--   nba.rankings_source  the same query, unchanged in meaning. The always-correct
--                        definition, and what the refresh reads.
--   nba.rankings         a materialized copy, refreshed after the post-game
--                        player_season_stats write. What the API reads.
--
-- The read path compares the copy's newest as_of_date with the newest snapshot
-- date in nba.player_season_stats and falls back to nba.rankings_source when
-- they differ, so a missed refresh costs latency, never correctness.
--
-- Three columns are new, and all three delete a second query from the request:
--   gp           was a separate DISTINCT ON scan over the whole season table
--   as_of_date   the snapshot date the row runs through (also the staleness signal)
--   season       was another ORDER BY as_of_date DESC LIMIT 1 probe
--
-- The unique index on id is what REFRESH MATERIALIZED VIEW CONCURRENTLY
-- requires; DISTINCT ON (player_id) joined 1:1 to nba.players guarantees it.

DROP VIEW IF EXISTS nba.rankings;

CREATE VIEW nba.rankings_source AS
WITH latest_per_player AS (
    -- Most recent season-stats snapshot per player, scoped to the current
    -- season. LEAD() is evaluated before DISTINCT ON filters rows, so the full
    -- per-player history is visible to the window function.
    SELECT DISTINCT ON (player_id)
        player_id,
        team_id,
        gp,
        fpts,
        as_of_date,
        season,
        LEAD(fpts, 5) OVER (
            PARTITION BY player_id
            ORDER BY as_of_date DESC
        ) AS fpts_5_ago
    FROM nba.player_season_stats
    WHERE season = (
        SELECT season
        FROM nba.player_season_stats
        ORDER BY as_of_date DESC
        LIMIT 1
    )
    ORDER BY player_id, as_of_date DESC
),
ranked AS (
    -- League-wide ranks for both current and historical fpts. For players with
    -- no history 5 snapshots ago (e.g. new players), COALESCE falls back to
    -- current fpts so rank_change comes out ~0.
    SELECT
        player_id,
        team_id,
        gp,
        fpts,
        as_of_date,
        season,
        RANK() OVER (ORDER BY fpts DESC)                                       AS curr_rank,
        RANK() OVER (ORDER BY COALESCE(fpts_5_ago, fpts) DESC)                 AS prev_rank
    FROM latest_per_player
)
SELECT
    p.id,
    r.curr_rank,
    p.name,
    r.team_id                                                                  AS team,
    r.fpts,
    ROUND(1.0 * r.fpts::numeric / NULLIF(r.gp, 0)::numeric, 2)                 AS avg_fpts,
    r.prev_rank - r.curr_rank                                                  AS rank_change,
    r.gp,
    r.as_of_date,
    r.season
FROM ranked r
JOIN nba.players p ON r.player_id = p.id
ORDER BY r.curr_rank;

CREATE MATERIALIZED VIEW nba.rankings AS
SELECT * FROM nba.rankings_source
WITH DATA;

CREATE UNIQUE INDEX rankings_id_key ON nba.rankings (id);

COMMENT ON MATERIALIZED VIEW nba.rankings IS
  'Materialized nba.rankings_source. Refreshed by the data-platform post-game player_season_stats pipeline; the API falls back to nba.rankings_source when this copy lags nba.player_season_stats.';
