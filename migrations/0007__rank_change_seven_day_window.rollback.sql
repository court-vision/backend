-- Back to migration 0006: rank_change versus five snapshots ago.

DROP MATERIALIZED VIEW IF EXISTS nba.rankings;
DROP VIEW IF EXISTS nba.rankings_source;

CREATE VIEW nba.rankings_source AS
WITH latest_per_player AS (
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
