-- The pickups a lane made stay in usr.scheduled_pickups (and their writes in
-- usr.roster_moves); they only lose the lane id.
ALTER TABLE usr.scheduled_pickups DROP COLUMN IF EXISTS lane_id;
DROP TABLE IF EXISTS usr.stream_lane_events;
DROP TABLE IF EXISTS usr.stream_lanes;
