-- Migration 0025: the permission a provider granted a connection.
--
-- Yahoo approves an app for Read or Read/Write in its developer console and
-- grants exactly that at the authorize step (`fspt-r` / `fspt-w`). Court
-- Vision holds Read as of 2026-09-15 with Read/Write requested, so a Yahoo
-- connection made today cannot write and one made after the upgrade can --
-- the row has to say which, or a lineup write is tried and refused. ESPN
-- cookies carry no such notion; the column stays NULL for them.
--
-- The callback also now keys a Yahoo row by the account guid the token
-- response carries (external_account_id, previously "" for every Yahoo row).
-- No data change here: the one production Yahoo connection is being
-- reconnected by hand, and rows written before this migration are re-keyed
-- by the P2 backfill (docs/YAHOO_PARITY_PLAN.md).
--
-- data-platform's copy of the ProviderConnection model will declare this
-- column: this must be applied (the backend deployed) before that model ships.

ALTER TABLE usr.provider_connections
    ADD COLUMN IF NOT EXISTS scope varchar(64);

COMMENT ON COLUMN usr.provider_connections.scope IS
  'The permission the provider granted these credentials, as it names it (Yahoo: fspt-r or fspt-w). NULL for ESPN and for rows written before 0025.';
