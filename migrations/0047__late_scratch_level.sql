-- Migration 0047: the Late scratches level below AI (the news plan's late-scratch
-- pass, step 3b; the Late scratches row of Dime's automations table).
--
-- A team's late scratches are off, notify (an email with the swap a verdict change
-- suggests: a ruled-out starter benched for a healthy bench player with a game, or a
-- cleared bench player started) or act (Court Vision makes that swap on ESPN and
-- emails it). AI stays in usr.autopilot.ai_rows (Dime's event check-ins).
--
-- It lives with the account's other automations, in the notification preferences:
-- the account's own level (default off, so nobody's lineup moves until they pick
-- it) and a team's override (null = the account's), as lineup's email and fill do.

ALTER TABLE usr.notification_preferences
    ADD COLUMN IF NOT EXISTS late_scratch_level varchar(8) NOT NULL DEFAULT 'off';
ALTER TABLE usr.notification_preferences
    DROP CONSTRAINT IF EXISTS notification_preferences_late_scratch_level_check;
ALTER TABLE usr.notification_preferences
    ADD CONSTRAINT notification_preferences_late_scratch_level_check
    CHECK (late_scratch_level IN ('off', 'notify', 'act'));

ALTER TABLE usr.notification_team_preferences
    ADD COLUMN IF NOT EXISTS late_scratch_level varchar(8);
ALTER TABLE usr.notification_team_preferences
    DROP CONSTRAINT IF EXISTS notification_team_preferences_late_scratch_level_check;
ALTER TABLE usr.notification_team_preferences
    ADD CONSTRAINT notification_team_preferences_late_scratch_level_check
    CHECK (late_scratch_level IS NULL OR late_scratch_level IN ('off', 'notify', 'act'));
