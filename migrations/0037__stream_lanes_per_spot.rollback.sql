-- Back to one open lane per team. Fails while a team has more than one open lane: stop the
-- extra lanes first.

DROP INDEX IF EXISTS usr.stream_lanes_one_per_spot;

CREATE UNIQUE INDEX IF NOT EXISTS stream_lanes_one_per_team
    ON usr.stream_lanes (team_id)
    WHERE status IN ('active', 'paused');
