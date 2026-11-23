-- Rollback 0033: back to the baseline's indexes, duplicates included.

CREATE INDEX IF NOT EXISTS idx_pipeline_runs_pipeline_name
    ON nba.pipeline_runs USING btree (pipeline_name);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_status
    ON nba.pipeline_runs USING btree (status);

DROP INDEX IF EXISTS nba.pipeline_runs_name_completed_success;
DROP INDEX IF EXISTS nba.pipeline_runs_name_started;
