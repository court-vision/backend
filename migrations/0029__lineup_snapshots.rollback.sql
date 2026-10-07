-- Rollback 0029: drop the lineup snapshot tables (child first).

DROP TABLE IF EXISTS usr.lineup_snapshot_players;
DROP TABLE IF EXISTS usr.lineup_snapshots;
