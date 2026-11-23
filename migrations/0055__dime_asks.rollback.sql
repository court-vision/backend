-- Rollback 0055: no more asks; the check-ins that ran them go too.
DROP TABLE IF EXISTS usr.ai_asks;
DELETE FROM usr.ai_checkins WHERE kind = 'ask';
ALTER TABLE usr.ai_checkins DROP CONSTRAINT IF EXISTS ai_checkins_kind_check;
ALTER TABLE usr.ai_checkins ADD CONSTRAINT ai_checkins_kind_check
    CHECK (kind IN ('morning', 'weekly', 'pre_lock', 'night', 'event'));
