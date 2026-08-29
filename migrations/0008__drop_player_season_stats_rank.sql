-- Migration 0008: drop nba.player_season_stats.rank.
--
-- ****  DO NOT DEPLOY THIS IN THE SAME RELEASE AS THE CODE THAT STOPPED  ****
-- ****  WRITING THE COLUMN. See migrations/README.md and                 ****
-- ****  docs/PRODUCTION_READINESS.md item 9.                             ****
--
-- Both services write nba.player_season_stats from their own copy of the Peewee
-- model. Backend applies migrations at startup, so shipping this alongside the
-- code change means the moment backend deploys, data-platform's still-running
-- previous image writes `rank` against a schema that no longer has it: the
-- post-game season-stats pipeline fails with UndefinedColumn and that night's
-- stats never land. Deploy the code first, confirm a clean post-game run, then
-- ship this.
--
-- What the column was: a rank among the players who happened to have a row
-- written on that snapshot date, ordered by CUMULATIVE SEASON fantasy points.
-- So rank=1 meant "the best season-long scorer among the players who played
-- that night" -- neither a standing nor a description of that night. Its two
-- readers (GET /v1/players/ and GET /v1/players/{id}/trends) both wanted a
-- league-wide rank and now read nba.rankings.curr_rank instead.
--
-- The two indexes exist only for this column, and both cost a write on every
-- one of the ~580 rows the post-game pipeline inserts nightly.

DROP INDEX IF EXISTS nba.playerseasonstats_as_of_date_rank;
DROP INDEX IF EXISTS nba.playerseasonstats_rank;

ALTER TABLE nba.player_season_stats DROP COLUMN IF EXISTS rank;
