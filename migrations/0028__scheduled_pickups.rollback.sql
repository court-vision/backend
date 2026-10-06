-- Restoring the two-value source CHECK fails while usr.roster_moves holds
-- 'scheduled' rows: delete them (or re-source them) first.
DROP TABLE IF EXISTS usr.scheduled_pickups;
ALTER TABLE usr.roster_moves DROP CONSTRAINT IF EXISTS roster_moves_source_check;
ALTER TABLE usr.roster_moves ADD CONSTRAINT roster_moves_source_check CHECK (source IN ('manual', 'auto'));
