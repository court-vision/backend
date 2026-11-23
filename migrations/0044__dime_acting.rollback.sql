ALTER TABLE usr.scheduled_pickups DROP COLUMN IF EXISTS made_by_autopilot;
UPDATE usr.roster_moves SET source = 'manual' WHERE source = 'autopilot';
ALTER TABLE usr.roster_moves DROP CONSTRAINT IF EXISTS roster_moves_source_check;
ALTER TABLE usr.roster_moves ADD CONSTRAINT roster_moves_source_check
    CHECK (source IN ('manual', 'auto', 'scheduled', 'availability'));
DROP TABLE IF EXISTS usr.autopilot_actions;
DROP TABLE IF EXISTS usr.ai_settings;
ALTER TABLE usr.autopilot DROP CONSTRAINT IF EXISTS autopilot_ai_rows_check;
ALTER TABLE usr.autopilot DROP COLUMN IF EXISTS restore;
ALTER TABLE usr.autopilot DROP COLUMN IF EXISTS autopilot;
ALTER TABLE usr.autopilot DROP COLUMN IF EXISTS ai_rows;
