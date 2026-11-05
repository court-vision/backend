DROP TABLE IF EXISTS usr.autopilot_events;
DROP INDEX IF EXISTS usr.ai_checkins_slot_uq;
DELETE FROM usr.ai_checkins WHERE kind = 'event';
CREATE UNIQUE INDEX IF NOT EXISTS ai_checkins_slot_uq
    ON usr.ai_checkins (team_id, kind, nba_date)
    WHERE NOT manual;
ALTER TABLE usr.ai_checkins DROP CONSTRAINT IF EXISTS ai_checkins_kind_check;
ALTER TABLE usr.ai_checkins ADD CONSTRAINT ai_checkins_kind_check
    CHECK (kind IN ('morning', 'weekly', 'pre_lock', 'night'));
