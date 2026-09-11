-- Drops 0024's status columns. The SWID normalization and any merge are not
-- reversed: the pasted spellings are gone, and a normalized key is what a
-- clean paste produced under 0005 anyway.
--
-- data-platform's ProviderConnection model declares these columns; roll it
-- back first, or its credential reads fail with UndefinedColumn.

ALTER TABLE usr.provider_connections DROP COLUMN IF EXISTS auth_failed_at;
ALTER TABLE usr.provider_connections DROP COLUMN IF EXISTS verified_at;
