-- Migration 0022: roster transactions share the lineup audit trail.
--
-- usr.roster_moves has recorded every lineup write (slot moves) Court Vision
-- sent to a provider. Add/drop transactions — the streamers page's "pick him
-- up" — go through the same writer, the same idempotency key discipline and
-- the same applied / applied_unverified / rejected / failed outcomes, so they
-- are rows here too rather than a second table with the same columns.
--
-- `kind` says which: 'lineup' rows keep their [{player_id, from_slot_id,
-- to_slot_id, role, note}] moves; 'transaction' rows carry
-- [{player_id, action: 'add' | 'drop', name}]. Existing rows are lineup
-- writes, hence the default. The auto-lineup dedup index (source = 'auto')
-- is untouched — transactions are manual only.

ALTER TABLE usr.roster_moves
    ADD COLUMN IF NOT EXISTS kind varchar(12) NOT NULL DEFAULT 'lineup';

ALTER TABLE usr.roster_moves
    DROP CONSTRAINT IF EXISTS roster_moves_kind_check;

ALTER TABLE usr.roster_moves
    ADD CONSTRAINT roster_moves_kind_check
    CHECK (kind IN ('lineup', 'transaction'));
