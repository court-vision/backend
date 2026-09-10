-- ESPN publishes two draft rankings in the same kona_player_info payload:
-- draftRanksByRankType.STANDARD (points leagues) and .ROTO (category leagues).
-- Only STANDARD was ever read. They disagree substantially — mean |delta| of 28
-- places over ESPN's own top 150, and Damian Lillard is STANDARD 66 / ROTO 310 --
-- so a category room reading the points list is reading the wrong board.
--
-- Auction values are per rank type too: the existing auction_value stays the
-- STANDARD one, and roto_auction_value is its category-league counterpart.

ALTER TABLE nba.draft_market
    ADD COLUMN IF NOT EXISTS roto_rank INTEGER,
    ADD COLUMN IF NOT EXISTS roto_auction_value NUMERIC(6, 1);

COMMENT ON COLUMN nba.draft_market.overall_rank IS
    'ESPN draftRanksByRankType.STANDARD.rank — the points-league draft ranking';
COMMENT ON COLUMN nba.draft_market.roto_rank IS
    'ESPN draftRanksByRankType.ROTO.rank — the category-league draft ranking';
