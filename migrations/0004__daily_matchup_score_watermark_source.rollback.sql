CREATE INDEX IF NOT EXISTS idx_daily_matchup_scores_pipeline_run_id
    ON stats_s2.daily_matchup_scores USING btree (pipeline_run_id);

ALTER TABLE stats_s2.daily_matchup_scores
    DROP COLUMN IF EXISTS scoring_period_source;
