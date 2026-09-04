DROP INDEX IF EXISTS usr.draft_sessions_user_espn_league_active_uq;
ALTER TABLE usr.draft_sessions
    DROP COLUMN IF EXISTS espn_league_id,
    DROP COLUMN IF EXISTS name;
