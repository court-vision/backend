-- Rollback 0047: late scratches go back to off everywhere (the previous code has no
-- level below AI for them).
ALTER TABLE usr.notification_team_preferences DROP COLUMN IF EXISTS late_scratch_level;
ALTER TABLE usr.notification_preferences DROP COLUMN IF EXISTS late_scratch_level;
