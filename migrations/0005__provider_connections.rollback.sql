DROP INDEX IF EXISTS usr.team_provider_connection_id;
ALTER TABLE usr.teams DROP COLUMN IF EXISTS provider_connection_id;
DROP TABLE IF EXISTS usr.provider_connections;
