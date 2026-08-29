-- Back to the plain view of migration 0002, columns included: a reader on the
-- old code path selects only the seven columns that existed then.

DROP MATERIALIZED VIEW IF EXISTS nba.rankings;
DROP VIEW IF EXISTS nba.rankings_source;

CREATE VIEW nba.rankings AS
WITH latest_per_player AS (
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
