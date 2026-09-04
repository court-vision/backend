-- Migration 0016: a draft room knows which ESPN draft feeds it, and has a name.
--
-- `espn_league_id` is the identity of the ESPN draft room a session follows:
-- set at creation for a live room (its team's league) and learned on the first
-- INIT for a mock room (an ESPN mock lobby carries a league id of its own).
-- Once set, the room refuses frames from any other ESPN draft, so a leftover
-- mock in the extension's log can no longer replay into a fresh room.
--
-- The partial unique index is the bijection: one ACTIVE room per user per ESPN
-- draft. Completed and abandoned rooms fall outside it, so next season's draft
-- in the same league gets a fresh room. `name` is the user's own label.

ALTER TABLE usr.draft_sessions
    ADD COLUMN IF NOT EXISTS espn_league_id bigint,
    ADD COLUMN IF NOT EXISTS name varchar(80);

CREATE UNIQUE INDEX IF NOT EXISTS draft_sessions_user_espn_league_active_uq
    ON usr.draft_sessions (user_id, espn_league_id)
    WHERE status = 'active' AND espn_league_id IS NOT NULL;
