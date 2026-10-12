-- Migration 0031: room for game history and the playoffs.
--
-- Until now nba.games and nba.player_game_stats held one season's regular
-- season (plus its preseason and the NBA Cup final). The Ask plan
-- (docs/ASK_PLAN.md) loads 2012-13 onward, playoffs and play-in included, into
-- the same two tables. That needs:
--
-- 1. The season type on both tables. It is not new data: a game id opens with
--    it (0022500123 = 2025-26 regular season, game 123). Both models derive it
--    from the id on every save (db/models/nba/games.py SEASON_TYPES), so the
--    CASE below must stay identical to that mapping. Fantasy readers keep to
--    'regular' and 'cup_final', which is exactly what they saw before.
-- 2. The season on box-score rows, also from the id, so a reader can bound a
--    query to one season without a join.
-- 3. Four box-score columns PlayerGameLogs has always sent and the pipeline
--    dropped: oreb, dreb, pf, plus_minus. NULL on existing rows until the
--    history load rewrites them.
--
-- It also repairs two things (a stale status, a wrong scale; see below).
--
-- nba.games: the weekly schedule sync ran on 2026-09-21 and
-- 2026-09-28 while production was still pinned to 2025-26, read the image's
-- static schedule copy (captured 2026-02-12) and set every game after it back
-- to 'scheduled'. Their scores were kept and every one has box scores, which
-- is the guard below. Game.upsert_game no longer moves a status backwards.

ALTER TABLE nba.games
    ADD COLUMN IF NOT EXISTS season_type varchar(12);

UPDATE nba.games
SET season_type = CASE left(game_id, 3)
        WHEN '001' THEN 'preseason'
        WHEN '002' THEN 'regular'
        WHEN '003' THEN 'all_star'
        WHEN '004' THEN 'playoffs'
        WHEN '005' THEN 'play_in'
        WHEN '006' THEN 'cup_final'
    END
WHERE season_type IS NULL;

UPDATE nba.games g
SET status = 'final'
WHERE g.season = '2025-26'
  AND g.game_id LIKE '002%'
  AND g.status = 'scheduled'
  AND g.game_date < DATE '2026-04-13'
  AND g.home_score IS NOT NULL
  AND g.away_score IS NOT NULL
  AND EXISTS (SELECT 1 FROM nba.player_game_stats p WHERE p.game_id = g.game_id);

ALTER TABLE nba.player_game_stats
    ADD COLUMN IF NOT EXISTS season varchar(7),
    ADD COLUMN IF NOT EXISTS season_type varchar(12),
    ADD COLUMN IF NOT EXISTS oreb smallint,
    ADD COLUMN IF NOT EXISTS dreb smallint,
    ADD COLUMN IF NOT EXISTS pf smallint,
    ADD COLUMN IF NOT EXISTS plus_minus smallint;

-- Same rules as season_for / season_type_for in db/models/nba/games.py.
UPDATE nba.player_game_stats
SET season = ((CASE WHEN substr(game_id, 4, 2)::int >= 46 THEN 1900 ELSE 2000 END)
                + substr(game_id, 4, 2)::int)::text
             || '-' || lpad(((substr(game_id, 4, 2)::int + 1) % 100)::text, 2, '0'),
    season_type = CASE left(game_id, 3)
        WHEN '001' THEN 'preseason'
        WHEN '002' THEN 'regular'
        WHEN '003' THEN 'all_star'
        WHEN '004' THEN 'playoffs'
        WHEN '005' THEN 'play_in'
        WHEN '006' THEN 'cup_final'
    END
WHERE game_id IS NOT NULL
  AND season IS NULL
  AND left(game_id, 3) IN ('001', '002', '003', '004', '005', '006')
  AND substr(game_id, 4, 2) ~ '^[0-9]{2}$';

-- nba.player_advanced_stats.tov_pct was stored as NBA sends TM_TOV_PCT,
-- turnovers per 100 plays (6.9), beside percentages that all come as fractions
-- (usg_pct 0.323), so every reader showed it 100x too big. The pipeline now
-- divides by 100 (data-platform). Every row here predates that: the pipeline
-- has not run since 2026-04-21 and runs again only from opening night.
UPDATE nba.player_advanced_stats
SET tov_pct = round(tov_pct / 100, 3)
WHERE tov_pct IS NOT NULL;

CREATE INDEX IF NOT EXISTS player_game_stats_season_type_idx
    ON nba.player_game_stats (season, season_type);

COMMENT ON COLUMN nba.games.season_type IS
  'From the game id prefix: preseason, regular, all_star, playoffs, play_in, cup_final.';
COMMENT ON COLUMN nba.player_game_stats.season IS
  'From the game id ("2025-26"). NULL while the row has no game.';
COMMENT ON COLUMN nba.player_game_stats.season_type IS
  'From the game id, as nba.games.season_type. Fantasy readers keep to regular and cup_final.';
