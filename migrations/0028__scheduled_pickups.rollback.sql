-- The rollback keeps the audit history: the writes scheduled pickups made stay
-- in usr.roster_moves under source 'scheduled'. The two-value source CHECK
-- comes back NOT VALID, so Postgres skips those rows and checks every new or
-- updated one; applying 0028 again re-adds (and validates) the three-value one.
DROP TABLE IF EXISTS usr.scheduled_pickups;
ALTER TABLE usr.roster_moves DROP CONSTRAINT IF EXISTS roster_moves_source_check;
ALTER TABLE usr.roster_moves ADD CONSTRAINT roster_moves_source_check CHECK (source IN ('manual', 'auto')) NOT VALID;
