-- Migration 0012: Draft Lab positions on the market snapshot.
--
-- The draft room counts ESPN's hard position caps (usr.leagues.position_limits) by
-- a player's *primary* position, and nba.players.position is nba_api-coarse ("G",
-- "G-F") — unusable for PG/SG/SF/PF/C caps. ESPN's own kona_player_info payload
-- carries the exact fields, so the preseason-market pipeline captures them onto the
-- daily snapshot: default_position_id (primary position), eligible_slot_ids (the
-- complete, authoritative eligibility list) and injury_status.
--
-- Two id spaces, deliberately stored in their native ones and never mixed:
-- default_position_id is 1-based (1=PG ... 5=C, the positionLimits space), while
-- eligible_slot_ids are 0-based lineup-slot ids (0=PG ... 4=C, 5=G, 6=F, 11=UT).
--
-- All nullable: snapshots written before the pipeline change simply lack them, and
-- the board falls back to the coarse nba.players.position until they arrive.

ALTER TABLE nba.draft_market
    ADD COLUMN IF NOT EXISTS default_position_id smallint,
    ADD COLUMN IF NOT EXISTS eligible_slot_ids   jsonb,
    ADD COLUMN IF NOT EXISTS injury_status       varchar(20);
