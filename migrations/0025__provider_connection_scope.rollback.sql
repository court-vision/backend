-- Drops 0025's scope column. data-platform's ProviderConnection model may
-- declare it; roll that back first, or its credential reads fail with
-- UndefinedColumn.

ALTER TABLE usr.provider_connections DROP COLUMN IF EXISTS scope;
