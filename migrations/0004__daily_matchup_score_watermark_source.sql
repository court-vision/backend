-- Migration 0004: provenance for stats_s2.daily_matchup_scores.scoring_period_id.
--
-- scoring_period_id is a provider-agnostic DAY watermark (1 = opening night,
-- the same integer space as ESPN's status.latestScoringPeriod): the first
-- season day NOT yet included in current_score. A snapshot at watermark B
-- therefore covers through day B-1, which is what lets the read path place the
-- live overlay without guessing at the provider's batch time.
--
-- ESPN reports the value directly. Yahoo has no such field, so it is derived
-- from our season calendar. Recording which one keeps the two from being
-- silently conflated if Yahoo's API ever grows a real day field.

ALTER TABLE stats_s2.daily_matchup_scores
    ADD COLUMN IF NOT EXISTS scoring_period_source text;

COMMENT ON COLUMN stats_s2.daily_matchup_scores.scoring_period_id IS
  'Day watermark (1 = opening night): first season day NOT included in current_score. NULL when unknown.';
COMMENT ON COLUMN stats_s2.daily_matchup_scores.scoring_period_source IS
  'Provenance of scoring_period_id: provider | calendar | NULL (legacy/unknown).';

-- Every existing non-null value came from ESPN's status.latestScoringPeriod;
-- the Yahoo extractor never set this column.
UPDATE stats_s2.daily_matchup_scores
   SET scoring_period_source = 'provider'
 WHERE scoring_period_id IS NOT NULL
   AND scoring_period_source IS NULL;

-- 0001__baseline.sql created two identical indexes on pipeline_run_id
-- (dailymatchupscore_pipeline_run_id and idx_daily_matchup_scores_pipeline_run_id).
-- Keep the Peewee-named one, which the model's index=True declares.
DROP INDEX IF EXISTS stats_s2.idx_daily_matchup_scores_pipeline_run_id;
