-- Rollback 0053: the analyst's notes, its reads and its spend line go together.
-- A note in the 'analyst' scope has no place under the two-scope check, and a
-- spend row with no user has none under NOT NULL.
DELETE FROM usr.ai_spend WHERE line = 'analyst';

ALTER TABLE usr.ai_spend
    DROP CONSTRAINT IF EXISTS ai_spend_user_check,
    DROP CONSTRAINT IF EXISTS ai_spend_line_check;
ALTER TABLE usr.ai_spend ADD CONSTRAINT ai_spend_line_check CHECK (line IN ('desk', 'autopilot'));
ALTER TABLE usr.ai_spend ALTER COLUMN user_id SET NOT NULL;

DROP TABLE IF EXISTS nba.analyst_reads;

-- Superseded rows point at their successors, so the scope's rows go in one statement
DELETE FROM nba.projection_adjustments WHERE scope = 'analyst';

ALTER TABLE nba.projection_adjustments
    DROP CONSTRAINT IF EXISTS projection_adjustments_season_note_check,
    DROP CONSTRAINT IF EXISTS projection_adjustments_scope_check;

ALTER TABLE nba.projection_adjustments
    ADD CONSTRAINT projection_adjustments_scope_check CHECK (scope IN ('preseason', 'season')),
    ADD CONSTRAINT projection_adjustments_season_note_check CHECK (
        (scope = 'season' AND review_date IS NOT NULL AND rates IS NULL)
        OR (scope = 'preseason' AND until_date IS NULL AND depends_on_player_id IS NULL
            AND return_earliest IS NULL AND return_latest IS NULL AND review_date IS NULL)
    );
