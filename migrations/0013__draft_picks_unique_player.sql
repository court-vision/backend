-- Migration 0013: one pick per player per draft session.
--
-- DraftService.add_pick refuses to record a player the session already holds;
-- this index settles the race two simultaneous clicks can open, the way
-- draft_picks_session_overall_uq does for the pick number. Partial on purpose:
-- a pick recorded before its player reached nba.players has a null player_id
-- and only a provider identity, which the service compares instead.

CREATE UNIQUE INDEX IF NOT EXISTS draft_picks_session_player_uq
    ON usr.draft_picks USING btree (session_id, player_id)
    WHERE player_id IS NOT NULL;
