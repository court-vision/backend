-- Migration 0050: when each team's first draft picks were protected for Dime (James,
-- 2026-11-15).
--
-- Dime's own rule never to drop one of the roster's top six by value gives way to the
-- user's protections (the roster lock, 0048). So that the cutover drops nothing Dime used
-- to keep, a team Dime acts on gets its first draft picks still on the roster protected,
-- once, when its next check-in starts (services/roster_protection.py `seed`). This is when
-- that happened: null until it has, so it runs once per team and never again after the
-- user unprotects one of them.

ALTER TABLE usr.autopilot ADD COLUMN IF NOT EXISTS protections_seeded_at timestamptz;
