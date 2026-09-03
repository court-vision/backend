-- Fails while keeper picks exist; delete or re-source them first.
ALTER TABLE usr.draft_picks DROP CONSTRAINT IF EXISTS draft_picks_source_check;
ALTER TABLE usr.draft_picks ADD CONSTRAINT draft_picks_source_check
    CHECK (source IN ('manual', 'espn_sync', 'import'));
