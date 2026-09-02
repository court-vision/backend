ALTER TABLE nba.draft_market
    DROP COLUMN IF EXISTS default_position_id,
    DROP COLUMN IF EXISTS eligible_slot_ids,
    DROP COLUMN IF EXISTS injury_status;
