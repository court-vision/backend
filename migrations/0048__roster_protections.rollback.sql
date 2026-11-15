-- Rollback 0048: no player is protected (the previous code has no roster lock).
DROP TABLE IF EXISTS usr.roster_protections;
