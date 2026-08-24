ALTER TABLE stats_s2.daily_matchup_scores DROP COLUMN IF EXISTS category_scores;
DROP INDEX IF EXISTS usr.team_league_id;
ALTER TABLE usr.teams DROP COLUMN IF EXISTS league_id;
DROP TABLE IF EXISTS usr.leagues;
