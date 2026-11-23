-- Rollback 0057: daily actions go back to off everywhere (the previous code has no
-- level below AI for them). Writes made under source 'daily_actions' are kept as
-- 'scheduled' (Court Vision's own timed writes, which the move ledger already files
-- as "Scheduled"), so the five-value source CHECK comes back validated and an
-- earlier rollback in the chain still applies.
UPDATE usr.roster_moves SET source = 'scheduled' WHERE source = 'daily_actions';
ALTER TABLE usr.roster_moves DROP CONSTRAINT IF EXISTS roster_moves_source_check;
ALTER TABLE usr.roster_moves ADD CONSTRAINT roster_moves_source_check
    CHECK (source IN ('manual', 'auto', 'scheduled', 'availability', 'autopilot'));

ALTER TABLE usr.notification_team_preferences DROP COLUMN IF EXISTS daily_actions_level;
ALTER TABLE usr.notification_preferences DROP COLUMN IF EXISTS daily_actions_level;
