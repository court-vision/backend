-- Migration 0021: a league-less draft room can say which format it drafts for.
--
-- A room's scoring has always come from its league: `resolve_scoring(league)`,
-- with the team's `scoring_preview` applied on top. A room with no team has no
-- league, so it resolved to `resolve_scoring(None)` — points, always. That made
-- the one thing a mock is for (practising a 9-cat draft, punts and all)
-- reachable only by borrowing a category league you already own.
--
-- `scoring_format` is that room's answer, and it is the same shape as a team's
-- `scoring_preview`: null means "points, the default", 'categories' means the
-- standard 9-cat. The CHECK keeps it from becoming a second source of truth —
-- a room WITH a league takes the league's format, and nothing else.

ALTER TABLE usr.draft_sessions
    ADD COLUMN IF NOT EXISTS scoring_format varchar(16);

ALTER TABLE usr.draft_sessions
    DROP CONSTRAINT IF EXISTS draft_sessions_scoring_format_check;

ALTER TABLE usr.draft_sessions
    ADD CONSTRAINT draft_sessions_scoring_format_check
    CHECK (
        scoring_format IS NULL
        OR (
            scoring_format IN ('points', 'categories')
            AND team_id IS NULL
            AND league_id IS NULL
        )
    );
