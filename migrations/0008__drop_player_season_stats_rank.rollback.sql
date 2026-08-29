-- Restores the column and its indexes, empty. The historical values are not
-- recoverable -- they were a per-night cohort artifact that nothing recomputes.

ALTER TABLE nba.player_season_stats ADD COLUMN IF NOT EXISTS rank smallint;

CREATE INDEX IF NOT EXISTS playerseasonstats_as_of_date_rank
    ON nba.player_season_stats USING btree (as_of_date, rank);
CREATE INDEX IF NOT EXISTS playerseasonstats_rank
    ON nba.player_season_stats USING btree (rank);
