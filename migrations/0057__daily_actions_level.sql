-- Migration 0057: the Daily actions level below AI (the Daily actions row of the
-- automations table at /account?v=automation).
--
-- Daily actions are the Week desk's list for the day: IR moves, and a free agent for
-- an open roster spot. A team's level below AI is off or act (Court Vision makes the
-- list's IR moves and fills open roster spots with the day's best free agent, never
-- dropping anyone, and emails what it did). notify (a morning email with the day's
-- list) is not built yet, but the column already takes it so building it needs no
-- migration. AI stays in usr.autopilot.ai_rows (Dime makes its own adds and drops).
--
-- It lives beside late_scratch_level (0047), in the notification preferences: the
-- account's own level (default off; since 0049 the Account desk only writes teams')
-- and a team's override (null = the account's).
--
-- usr.roster_moves gains the source 'daily_actions' for the writes that pass makes
-- (its IR moves, kind lineup; its adds, kind transaction).

ALTER TABLE usr.notification_preferences
    ADD COLUMN IF NOT EXISTS daily_actions_level varchar(8) NOT NULL DEFAULT 'off';
ALTER TABLE usr.notification_preferences
    DROP CONSTRAINT IF EXISTS notification_preferences_daily_actions_level_check;
ALTER TABLE usr.notification_preferences
    ADD CONSTRAINT notification_preferences_daily_actions_level_check
    CHECK (daily_actions_level IN ('off', 'notify', 'act'));

ALTER TABLE usr.notification_team_preferences
    ADD COLUMN IF NOT EXISTS daily_actions_level varchar(8);
ALTER TABLE usr.notification_team_preferences
    DROP CONSTRAINT IF EXISTS notification_team_preferences_daily_actions_level_check;
ALTER TABLE usr.notification_team_preferences
    ADD CONSTRAINT notification_team_preferences_daily_actions_level_check
    CHECK (daily_actions_level IS NULL OR daily_actions_level IN ('off', 'notify', 'act'));

ALTER TABLE usr.roster_moves
    DROP CONSTRAINT IF EXISTS roster_moves_source_check;
ALTER TABLE usr.roster_moves
    ADD CONSTRAINT roster_moves_source_check
    CHECK (source IN ('manual', 'auto', 'scheduled', 'availability', 'autopilot', 'daily_actions'));
