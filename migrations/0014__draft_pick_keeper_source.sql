-- Migration 0014: keeper picks.
--
-- A keeper is a pick spent before the draft starts. DraftService records it
-- with source 'keeper' at the pick its round costs, so it leaves the board like
-- any pick while whose-turn arithmetic steps over it. The column's CHECK was
-- declared inline in 0010, which Postgres named draft_picks_source_check.

ALTER TABLE usr.draft_picks DROP CONSTRAINT IF EXISTS draft_picks_source_check;
ALTER TABLE usr.draft_picks ADD CONSTRAINT draft_picks_source_check
    CHECK (source IN ('manual', 'espn_sync', 'import', 'keeper'));
