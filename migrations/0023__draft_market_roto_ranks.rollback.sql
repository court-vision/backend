ALTER TABLE nba.draft_market
    DROP COLUMN IF EXISTS roto_rank,
    DROP COLUMN IF EXISTS roto_auction_value;
