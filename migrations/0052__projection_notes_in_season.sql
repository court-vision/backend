-- Notes in season on nba.projection_adjustments (the Live rankings plan, step 3).
--
-- Until now the table held one kind of judgment: a season-long input to the
-- preseason projection (55 reviewed calls for 2026-27), read by cv-projection
-- before opening night. In season the live projection (source 'cv_live')
-- needs a different kind: a fact with an end. "Out until about Dec 8", "32
-- minutes while the starter is out", "on a minutes limit for two weeks".
--
-- scope
--   'preseason': what the table held before. cv-projection reads these, and
--                only these, so an in-season note can never rewrite the prior.
--   'season':    a dated note read by the live projection alone. One live note
--                per player per season, beside his preseason adjustment.
--
-- What a season note adds to the numbers the table already had (minutes, games,
-- return_date, usage):
--   until_date            the role change (minutes, usage) holds through this day
--   depends_on_player_id  ... or until this teammate plays again
--   return_earliest/_latest  the range around return_date; the price uses return_date
--   evidence              what it rests on: a list of stored items, each kept
--                         as written (source, headline, url, time) so the note
--                         can be explained after the item is gone
--   confidence            confirmed | likely | possible
--   review_date           read again by this day. Past it the note is no longer
--                         applied: every season note expires.
--
-- Nothing is written when a note ends. Whether a live note is still in force on
-- a given morning (its date, its teammate, its review date, his own return) is
-- worked out by the reader, so the table stays append-only and any past
-- morning can be rebuilt.

ALTER TABLE nba.projection_adjustments
    ADD COLUMN IF NOT EXISTS scope                VARCHAR(10) NOT NULL DEFAULT 'preseason',
    ADD COLUMN IF NOT EXISTS until_date           DATE,
    ADD COLUMN IF NOT EXISTS depends_on_player_id INTEGER REFERENCES nba.players (id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS return_earliest      DATE,
    ADD COLUMN IF NOT EXISTS return_latest        DATE,
    ADD COLUMN IF NOT EXISTS evidence             JSONB,
    ADD COLUMN IF NOT EXISTS confidence           VARCHAR(10),
    ADD COLUMN IF NOT EXISTS review_date          DATE;

ALTER TABLE nba.projection_adjustments
    DROP CONSTRAINT IF EXISTS projection_adjustments_scope_check,
    DROP CONSTRAINT IF EXISTS projection_adjustments_confidence_check,
    DROP CONSTRAINT IF EXISTS projection_adjustments_return_range_check,
    DROP CONSTRAINT IF EXISTS projection_adjustments_depends_on_other_check,
    DROP CONSTRAINT IF EXISTS projection_adjustments_season_note_check;

ALTER TABLE nba.projection_adjustments
    ADD CONSTRAINT projection_adjustments_scope_check CHECK (scope IN ('preseason', 'season')),
    ADD CONSTRAINT projection_adjustments_confidence_check CHECK (
        confidence IS NULL OR confidence IN ('confirmed', 'likely', 'possible')
    ),
    -- A range is around a date: earliest <= return_date <= latest
    ADD CONSTRAINT projection_adjustments_return_range_check CHECK (
        (return_earliest IS NULL AND return_latest IS NULL)
        OR (return_date IS NOT NULL
            AND (return_earliest IS NULL OR return_earliest <= return_date)
            AND (return_latest IS NULL OR return_latest >= return_date))
    ),
    ADD CONSTRAINT projection_adjustments_depends_on_other_check CHECK (
        depends_on_player_id IS NULL OR depends_on_player_id <> player_id
    ),
    -- A season note always has a day it is read again, and sets no per-stat
    -- multiplier (the live projection prices minutes, usage and games). A
    -- preseason adjustment has none of the in-season fields.
    ADD CONSTRAINT projection_adjustments_season_note_check CHECK (
        (scope = 'season' AND review_date IS NOT NULL AND rates IS NULL)
        OR (scope = 'preseason' AND until_date IS NULL AND depends_on_player_id IS NULL
            AND return_earliest IS NULL AND return_latest IS NULL AND review_date IS NULL)
    );

-- One live row per player per season per scope: a season note sits beside the
-- preseason adjustment and neither supersedes the other.
DROP INDEX IF EXISTS nba.projection_adjustments_one_active;
CREATE UNIQUE INDEX IF NOT EXISTS projection_adjustments_one_active_per_scope
    ON nba.projection_adjustments (player_id, season, scope)
    WHERE superseded_by IS NULL AND retired_at IS NULL;

-- The live projection reads every live season note each morning
CREATE INDEX IF NOT EXISTS projection_adjustments_season_scope_idx
    ON nba.projection_adjustments (season, scope);
