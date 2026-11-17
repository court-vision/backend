-- Back to one live adjustment per player per season. The season notes go with
-- the columns that made them: a note without its end date is not a preseason
-- adjustment, and two live rows for one player would not fit the old index.
DELETE FROM nba.projection_adjustments WHERE scope = 'season';

DROP INDEX IF EXISTS nba.projection_adjustments_season_scope_idx;
DROP INDEX IF EXISTS nba.projection_adjustments_one_active_per_scope;

ALTER TABLE nba.projection_adjustments
    DROP CONSTRAINT IF EXISTS projection_adjustments_season_note_check,
    DROP CONSTRAINT IF EXISTS projection_adjustments_depends_on_other_check,
    DROP CONSTRAINT IF EXISTS projection_adjustments_return_range_check,
    DROP CONSTRAINT IF EXISTS projection_adjustments_confidence_check,
    DROP CONSTRAINT IF EXISTS projection_adjustments_scope_check;

ALTER TABLE nba.projection_adjustments
    DROP COLUMN IF EXISTS review_date,
    DROP COLUMN IF EXISTS confidence,
    DROP COLUMN IF EXISTS evidence,
    DROP COLUMN IF EXISTS return_latest,
    DROP COLUMN IF EXISTS return_earliest,
    DROP COLUMN IF EXISTS depends_on_player_id,
    DROP COLUMN IF EXISTS until_date,
    DROP COLUMN IF EXISTS scope;

CREATE UNIQUE INDEX IF NOT EXISTS projection_adjustments_one_active
    ON nba.projection_adjustments (player_id, season)
    WHERE superseded_by IS NULL AND retired_at IS NULL;
