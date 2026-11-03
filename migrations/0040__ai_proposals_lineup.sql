-- Migration 0040: lineup chips (the CV AI plan, § 6: step 5, lineup moves).
--
-- A proposal can now be a set of slot moves on one day's board (`kind = 'lineup'`,
-- made by `propose_lineup`): start, bench and shift moves the server checked with
-- the Week desk's own `validate_moves`. The chip is sent through the Week desk's
-- lineup write (`POST /teams/{id}/lineup/moves`) with the proposal's id, for today's
-- board or a later day's, and the route marks it sent with the write's audit id.
--
-- `moves` holds the moves as the write takes them and the chip shows them: ESPN
-- player id, NBA id, name, team, from / to slot id and name, role (start | bench |
-- shift) and the planner's note. A lineup row has no add or drop; a transaction or
-- scheduled row has no moves.

ALTER TABLE usr.ai_proposals ADD COLUMN IF NOT EXISTS moves jsonb;

ALTER TABLE usr.ai_proposals DROP CONSTRAINT IF EXISTS ai_proposals_kind_check;
ALTER TABLE usr.ai_proposals ADD CONSTRAINT ai_proposals_kind_check
    CHECK (kind IN ('transaction', 'scheduled', 'lineup'));

ALTER TABLE usr.ai_proposals DROP CONSTRAINT IF EXISTS ai_proposals_move_check;
ALTER TABLE usr.ai_proposals ADD CONSTRAINT ai_proposals_move_check
    CHECK ((kind = 'lineup' AND moves IS NOT NULL)
           OR (kind <> 'lineup' AND (add_player_id IS NOT NULL OR drop_player_id IS NOT NULL)));

COMMENT ON COLUMN usr.ai_proposals.moves IS
  'Lineup chips only: the slot moves (ESPN player id, NBA id, name, team, from/to slot id and name, role, note) as the lineup write takes them.';
