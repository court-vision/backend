-- Database roles the backend needs beyond its own connection.
--
-- Run once per database (production, staging) as a superuser, in psql: the
-- \password line prompts for the password and never writes it anywhere, which
-- is why this is a psql script rather than a migration (see
-- docs/PENDING_PROD_CHECKS.md on why a role script must not ship a literal
-- password). The grants on the views are repeated by migration 0036 and by
-- any later migration that recreates them, so a database migrated before the
-- role existed catches up the next time those run; the ones below cover a
-- database migrated first.
--
--   psql "$DATABASE_URL" -f ops/roles.sql
--
-- Then set ASK_READER_DATABASE_URL on the api and api-staging services to
-- postgresql://ask_reader:<password>@<host>:<port>/<database>, the same host
-- and database as DATABASE_URL. Unset, the backend runs query_stats on its
-- main pool inside a read-only transaction with the same 2 s timeout, and
-- logs that it is doing so.
--
-- ask_reader: what the AI agent's query_stats tool runs as (services/ai/stats/
-- reader.py; docs/QUERY_STATS_PLAN.md). The server writes every statement it
-- runs, so this role is defence in depth: a compiler bug must still be unable
-- to read usr.* or to hold a connection for 30 s.
--   - read-only by default and a 2 s statement timeout, at the role level so
--     a connection that forgot to set them gets them anyway;
--   - USAGE on schema nba and SELECT on the two nba.ask_* views only. Views
--     run with their owner's privileges, so the role reads the tables under
--     them through the views and nowhere else;
--   - a connection limit a little above the backend's reader pool (4 per
--     process) so a leak shows up as a refused connection, not a drained
--     database.

CREATE ROLE ask_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION
    CONNECTION LIMIT 12;
\password ask_reader

ALTER ROLE ask_reader SET default_transaction_read_only = on;
ALTER ROLE ask_reader SET statement_timeout = '2s';
ALTER ROLE ask_reader SET idle_in_transaction_session_timeout = '5s';

GRANT USAGE ON SCHEMA nba TO ask_reader;
GRANT SELECT ON nba.ask_player_games, nba.ask_player_seasons TO ask_reader;

-- What the role can see, for the log. Expect exactly the two views.
SELECT table_schema, table_name, privilege_type
FROM information_schema.table_privileges
WHERE grantee = 'ask_reader'
ORDER BY 1, 2;
