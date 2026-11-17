-- Rollback 0050: the previous code has no seed. The protections it made stay: they are
-- ordinary protections the user can remove.
ALTER TABLE usr.autopilot DROP COLUMN IF EXISTS protections_seeded_at;
