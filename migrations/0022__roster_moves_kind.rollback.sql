ALTER TABLE usr.roster_moves DROP CONSTRAINT IF EXISTS roster_moves_kind_check;
ALTER TABLE usr.roster_moves DROP COLUMN IF EXISTS kind;
