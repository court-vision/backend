-- Migration 0024: ESPN connections a user can manage directly.
--
-- 0005 stored credentials once per (user, provider, provider-side account) --
-- for ESPN, once per SWID -- and linked each team to its row. Two gaps kept
-- that row from being something the UI could offer as "your ESPN account":
--
-- 1. The key was the SWID exactly as pasted. ESPN issues it as an upper-case
--    GUID in braces, but a lower-case or brace-less copy made a second row for
--    the same account, and refreshing the cookies through one team then
--    updated that row alone while the account's other teams kept the stale
--    one. credential_service.normalize_swid now normalizes the key on write;
--    this brings the existing rows in line. Rows that collide once normalized
--    are merged into the most recently updated one -- it holds the newest
--    cookies -- and their teams move with them.
--
-- 2. Nothing recorded whether the stored cookies still work. verified_at and
--    auth_failed_at hold the provider's last verdicts from an explicit check,
--    so an expired connection can be shown once rather than failing on every
--    team.
--
-- data-platform's copy of the ProviderConnection model declares both columns:
-- this must be applied (the backend deployed) before that model ships.

ALTER TABLE usr.provider_connections
    ADD COLUMN IF NOT EXISTS verified_at    timestamp with time zone,
    ADD COLUMN IF NOT EXISTS auth_failed_at timestamp with time zone;

COMMENT ON COLUMN usr.provider_connections.verified_at IS
  'When the provider last accepted these credentials in an explicit check. NULL = not checked since they were saved.';
COMMENT ON COLUMN usr.provider_connections.auth_failed_at IS
  'When the provider last rejected these credentials. Cleared when they are replaced.';

-- The same rule as credential_service.normalize_swid: drop whitespace and
-- braces, upper-case, re-brace. A session temp table rather than ON COMMIT
-- DROP, so this runs the same whether or not it is inside one transaction.
DROP TABLE IF EXISTS pg_temp.espn_connection_keys;

CREATE TEMP TABLE espn_connection_keys AS
SELECT id,
       user_id,
       normalized,
       row_number() OVER (PARTITION BY user_id, normalized ORDER BY updated_at DESC, id DESC) AS recency
FROM (
    SELECT id,
           user_id,
           updated_at,
           '{' || upper(regexp_replace(external_account_id, '[[:space:]{}]', '', 'g')) || '}' AS normalized
    FROM usr.provider_connections
    WHERE provider = 'espn'
) keyed
WHERE normalized <> '{}';

-- Teams on a duplicate follow it to the survivor first, so the delete below
-- (ON DELETE SET NULL) leaves none of them unlinked.
UPDATE usr.teams t
SET provider_connection_id = survivor.id
FROM espn_connection_keys duplicate
JOIN espn_connection_keys survivor
  ON survivor.user_id = duplicate.user_id
 AND survivor.normalized = duplicate.normalized
 AND survivor.recency = 1
WHERE duplicate.recency > 1
  AND t.provider_connection_id = duplicate.id;

DELETE FROM usr.provider_connections c
USING espn_connection_keys duplicate
WHERE duplicate.recency > 1
  AND c.id = duplicate.id;

UPDATE usr.provider_connections c
SET external_account_id = k.normalized
FROM espn_connection_keys k
WHERE k.recency = 1
  AND c.id = k.id
  AND c.external_account_id <> k.normalized;

DROP TABLE pg_temp.espn_connection_keys;
