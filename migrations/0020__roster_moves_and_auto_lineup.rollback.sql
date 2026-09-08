ALTER TABLE usr.notification_team_preferences DROP COLUMN IF EXISTS auto_lineup_enabled;
ALTER TABLE usr.notification_preferences DROP COLUMN IF EXISTS auto_lineup_enabled;
DROP INDEX IF EXISTS usr.roster_moves_auto_once_per_day_uq;
DROP INDEX IF EXISTS usr.roster_moves_team_date;
DROP TABLE IF EXISTS usr.roster_moves;
