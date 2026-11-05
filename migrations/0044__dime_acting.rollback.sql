ALTER TABLE usr.scheduled_pickups DROP COLUMN IF EXISTS made_by_autopilot;
UPDATE usr.roster_moves SET source = 'manual' WHERE source = 'autopilot';
ALTER TABLE usr.roster_moves DROP CONSTRAINT IF EXISTS roster_moves_source_check;
ALTER TABLE usr.roster_moves ADD CONSTRAINT roster_moves_source_check
    CHECK (source IN ('manual', 'auto', 'scheduled', 'availability'));
DROP TABLE IF EXISTS usr.autopilot_actions;
ALTER TABLE usr.autopilot DROP CONSTRAINT IF EXISTS autopilot_modes_check;
ALTER TABLE usr.autopilot DROP COLUMN IF EXISTS adds_drops_mode;
ALTER TABLE usr.autopilot DROP COLUMN IF EXISTS streaming_mode;
ALTER TABLE usr.autopilot DROP COLUMN IF EXISTS lineup_mode;
ALTER TABLE usr.autopilot DROP COLUMN IF EXISTS mode;
