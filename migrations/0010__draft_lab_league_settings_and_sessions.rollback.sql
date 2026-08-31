DROP TABLE IF EXISTS usr.draft_picks;
DROP TABLE IF EXISTS usr.draft_sessions;

ALTER TABLE usr.leagues
    DROP COLUMN IF EXISTS position_limits,
    DROP COLUMN IF EXISTS draft_settings;
