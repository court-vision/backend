-- Migration 0016: a draft room knows which ESPN draft feeds it, and has a name.
--
-- `espn_league_id` is the identity of the ESPN draft room a session follows:
-- set at creation for a live room (its team's league) and learned on the first
-- INIT for a mock room (an ESPN mock lobby carries a league id of its own).
-- Once set, the room refuses frames from any other ESPN draft, so a leftover
-- mock in the extension's log can no longer replay into a fresh room.
--
-- The partial unique index is the bijection: one ACTIVE room per user per ESPN
-- draft. Completed and abandoned rooms fall outside it, so next season's draft
-- in the same league gets a fresh room. `name` is the user's own label.

ALTER TABLE usr.draft_sessions
    ADD COLUMN IF NOT EXISTS espn_league_id bigint,
    ADD COLUMN IF NOT EXISTS name varchar(80);

-- Rooms that followed a league before linking existed: a live room made from
-- an ESPN team is that league's room, so it gets the link here — otherwise the
-- exclusivity check would not see it, and its next INIT would be refused in
-- favour of a newer room.
UPDATE usr.draft_sessions s
   SET espn_league_id = l.provider_league_id::bigint
  FROM usr.leagues l
 WHERE s.league_id = l.id
   AND s.kind = 'live'
   AND s.espn_league_id IS NULL
   AND l.provider = 'espn'
   AND l.provider_league_id ~ '^[0-9]+$';

-- Where that leaves two ACTIVE rooms on one draft, the most recently touched
-- keeps the link and the others go back to unlinked: still active, nothing
-- deleted, and their next INIT names the room that has the draft.
UPDATE usr.draft_sessions s
   SET espn_league_id = NULL
 WHERE s.status = 'active'
   AND s.espn_league_id IS NOT NULL
   AND EXISTS (
       SELECT 1
         FROM usr.draft_sessions o
        WHERE o.user_id = s.user_id
          AND o.espn_league_id = s.espn_league_id
          AND o.status = 'active'
          AND (o.updated_at > s.updated_at OR (o.updated_at = s.updated_at AND o.id > s.id))
   );

CREATE UNIQUE INDEX IF NOT EXISTS draft_sessions_user_espn_league_active_uq
    ON usr.draft_sessions (user_id, espn_league_id)
    WHERE status = 'active' AND espn_league_id IS NOT NULL;
