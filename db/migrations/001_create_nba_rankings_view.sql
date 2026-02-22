-- Migration 001: Create nba.rankings view
--
-- Replaces stats_s2.standings with a view backed by nba.player_season_stats.
--
-- Key differences from the old standings view:
--   - curr_rank is a true league-wide rank (RANK() over all players by fpts),
--     not the per-day rank stored in player_season_stats.rank which only covers
--     players who happened to play on that specific date.
--   - prev_rank is similarly computed league-wide using each player's fpts from
--     ~5 snapshots ago, so rank_change reflects real movement in the overall
--     standings rather than same-day cohort movement.
--   - Season is derived dynamically from the most recent as_of_date so the view
--     never needs to be updated when a new season starts.

DROP VIEW IF EXISTS nba.rankings;

CREATE VIEW nba.rankings AS
WITH latest_per_player AS (
    -- Get the most recent season-stats snapshot per player, scoped to the
    -- current season. LEAD() is evaluated before DISTINCT ON filters rows,
    -- so the full per-player history is visible for the window function.
    SELECT DISTINCT ON (player_id)
        player_id,
        team_id,
        gp,
        fpts,
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
    -- Compute league-wide ranks for both current and historical fpts.
    -- For players with no history 5 snapshots ago (e.g. new players),
    -- COALESCE falls back to current fpts so rank_change comes out ~0.
    SELECT
        player_id,
        team_id,
        gp,
        fpts,
        RANK() OVER (ORDER BY fpts DESC)                                   AS curr_rank,
        RANK() OVER (ORDER BY COALESCE(fpts_5_ago, fpts) DESC)             AS prev_rank
    FROM latest_per_player
)
SELECT
    p.id,
    r.curr_rank,
    p.name,
    r.team_id                                                              AS team,
    r.fpts,
    ROUND(1.0 * r.fpts::numeric / NULLIF(r.gp, 0)::numeric, 2)           AS avg_fpts,
    r.prev_rank - r.curr_rank                                             AS rank_change
FROM ranked r
JOIN nba.players p ON r.player_id = p.id
ORDER BY r.curr_rank;
