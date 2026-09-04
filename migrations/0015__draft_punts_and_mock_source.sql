-- Migration 0015: punt builds, and picks made by the mock autopicker.
--
-- `punts` is the list of category keys a room is deliberately conceding. It
-- belongs on the session rather than in a query param: recommendations are
-- computed server-side, a reload mid-draft must not forget the build, and the
-- recap should know what the draft was played for. Validated against the
-- league's own rankable categories before it is written (DraftService).
--
-- `mock` joins the source vocabulary here rather than with the autopicker that
-- writes it: the CHECK has to be live in the database before any code can
-- insert one, and only the backend applies migrations.

ALTER TABLE usr.draft_sessions
    ADD COLUMN IF NOT EXISTS punts jsonb NOT NULL DEFAULT '[]'::jsonb;

ALTER TABLE usr.draft_picks DROP CONSTRAINT IF EXISTS draft_picks_source_check;
ALTER TABLE usr.draft_picks ADD CONSTRAINT draft_picks_source_check
    CHECK (source IN ('manual', 'espn_sync', 'import', 'keeper', 'mock'));
