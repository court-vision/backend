DELETE FROM usr.ai_proposals WHERE kind = 'lineup';
ALTER TABLE usr.ai_proposals DROP CONSTRAINT IF EXISTS ai_proposals_move_check;
ALTER TABLE usr.ai_proposals ADD CONSTRAINT ai_proposals_move_check
    CHECK (add_player_id IS NOT NULL OR drop_player_id IS NOT NULL);
ALTER TABLE usr.ai_proposals DROP CONSTRAINT IF EXISTS ai_proposals_kind_check;
ALTER TABLE usr.ai_proposals ADD CONSTRAINT ai_proposals_kind_check
    CHECK (kind IN ('transaction', 'scheduled'));
ALTER TABLE usr.ai_proposals DROP COLUMN IF EXISTS moves;
