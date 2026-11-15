-- Rollback 0049: the account's auto-lineup and Late scratches levels come back as they
-- were. The values copied onto each team stay: they are the account's, so no team does
-- anything different.
UPDATE usr.notification_preferences p
SET auto_lineup_enabled = a.auto_lineup_enabled, late_scratch_level = a.late_scratch_level
FROM usr.account_automation_0049 a
WHERE a.user_id = p.user_id;
DROP TABLE IF EXISTS usr.account_automation_0049;
