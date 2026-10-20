-- Several stream lanes per team, one per spot: the planner plans a team's lanes together
-- (services/stream_lane_service.py caps them at four). An open lane is unique by the player
-- holding its spot instead of by team.

DROP INDEX IF EXISTS usr.stream_lanes_one_per_team;

CREATE UNIQUE INDEX IF NOT EXISTS stream_lanes_one_per_spot
    ON usr.stream_lanes (team_id, holder_player_id)
    WHERE status IN ('active', 'paused');
