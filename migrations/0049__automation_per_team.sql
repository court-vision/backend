-- Migration 0049: AI and automation is set per team (the Account desk's AI and
-- automation sheet, 2026-11-15).
--
-- The sheet has no account-level table any more: each team holds its own Lineup level
-- (the tip-off email with auto-lineup) and its own Late scratches level, next to its AI
-- levels and the autopilot switch. So that nothing a team does changes:
--
-- 1. every team whose account had auto-lineup on, or Late scratches above off, takes
--    the account's value wherever it has none of its own (an override row is added
--    where the team had none; its notification fields stay null, the account's);
-- 2. a team on autopilot whose `restore` remembered auto-lineup as the account's (null)
--    remembers the account's value instead, so switching autopilot off puts back what
--    the team had;
-- 3. the account's auto-lineup and Late scratches go back to their defaults (off), so a
--    team added later starts with nothing acting on ESPN until it is set on that team.
--
-- The account's tip-off email (lineup_alerts_enabled) belongs to the Notifications
-- sheet, which keeps its account setting and team overrides, and is left as it is.
-- The account's values before the reset are kept in usr.account_automation_0049 for
-- the rollback; a later migration can drop it.

CREATE TABLE IF NOT EXISTS usr.account_automation_0049 (
    user_id             integer PRIMARY KEY,
    auto_lineup_enabled boolean NOT NULL,
    late_scratch_level  varchar(8) NOT NULL
);

INSERT INTO usr.account_automation_0049 (user_id, auto_lineup_enabled, late_scratch_level)
SELECT p.user_id, p.auto_lineup_enabled, p.late_scratch_level
FROM usr.notification_preferences p
WHERE p.auto_lineup_enabled OR p.late_scratch_level <> 'off'
ON CONFLICT (user_id) DO NOTHING;

INSERT INTO usr.notification_team_preferences AS tp
    (user_id, team_id, auto_lineup_enabled, late_scratch_level, created_at, updated_at)
SELECT t.user_id, t.team_id, a.auto_lineup_enabled, a.late_scratch_level,
       now() AT TIME ZONE 'UTC', now() AT TIME ZONE 'UTC'
FROM usr.teams t
JOIN usr.account_automation_0049 a ON a.user_id = t.user_id
ON CONFLICT (user_id, team_id) DO UPDATE SET
    auto_lineup_enabled = COALESCE(tp.auto_lineup_enabled, EXCLUDED.auto_lineup_enabled),
    late_scratch_level  = COALESCE(tp.late_scratch_level, EXCLUDED.late_scratch_level);

UPDATE usr.autopilot ap
SET restore = jsonb_set(ap.restore, '{lineup,auto_lineup_enabled}', to_jsonb(a.auto_lineup_enabled))
FROM usr.teams t
JOIN usr.account_automation_0049 a ON a.user_id = t.user_id
WHERE ap.team_id = t.team_id
  AND jsonb_typeof(ap.restore -> 'lineup') = 'object'
  AND COALESCE(jsonb_typeof(ap.restore -> 'lineup' -> 'auto_lineup_enabled'), 'null') = 'null';

UPDATE usr.notification_preferences p
SET auto_lineup_enabled = false, late_scratch_level = 'off'
FROM usr.account_automation_0049 a
WHERE a.user_id = p.user_id;
