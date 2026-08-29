-- Migration 0009: a durable record of what each pipeline batch decided.
--
-- `nba.pipeline_runs` records runs that happened. Nothing records the runs that
-- *didn't* — and the whole failure mode this table exists for is a night where
-- the batch endpoint decided, 48 times in a row, that there was nothing to do.
-- The only trace of that decision today is a log line, and the batch itself
-- (the in-memory JobManager in `core/job_manager.py`) is gone on restart.
--
-- PENDING_PROD_CHECKS #3 is the worked example: 13 of 66 matchup periods had no
-- snapshot until day 1 or day 5, meaning `daily_matchup_scores` was not running
-- for days at a stretch, and the evidence for that had to be reconstructed
-- afterwards from the shape of the data it failed to write.
--
-- One row per batch invocation that got as far as deciding per-pipeline, plus
-- one row per night when the post-game window closes (`decision =
-- 'window_closed'`), which is where the completeness check lives. Polls that
-- stop at an earlier gate ("no games", "before the window") are not recorded
-- here — `nba.cron_job_runs` already holds one row per trigger attempt with the
-- response body, so recording them again would be ~90 rows a day of noise.
--
-- Additive, and nothing reads it but the data-platform. Deploy order still
-- matters in one direction: data-platform writes this table, so the backend
-- (which applies migrations) must ship first. A missing table degrades to no
-- batch records, never to a failed pipeline — see `PipelineBatch.open`.

CREATE TABLE IF NOT EXISTS nba.pipeline_batches (
    id            uuid        PRIMARY KEY,

    -- 'pre_game' | 'post_game' — the PipelineCategory the batch dispatches.
    category      varchar(20) NOT NULL,

    -- The batch's NBA game date, on the 6 AM ET rule. This is the date handed
    -- to every pipeline in the batch (PipelineContext.nba_date), so a batch
    -- cannot straddle the day boundary and stamp its rows with two dates.
    nba_date      date        NOT NULL,

    triggered_at  timestamptz NOT NULL,
    completed_at  timestamptz,

    -- 'dispatched' | 'all_skipped' | 'window_closed'
    decision      varchar(20) NOT NULL,
    -- Stable slug, mostly from pipelines/gates.py. Interface, not prose.
    reason        varchar(64) NOT NULL,

    -- ?force=true or ?date=: gates bypassed, so this row is not evidence about
    -- the schedule.
    forced        boolean     NOT NULL DEFAULT false,

    -- The in-memory JobManager id, for correlating with GET /jobs/{id} while
    -- the process is still up.
    job_id        varchar(64),

    -- pipeline name -> {decision, reason, status, records}. Written at dispatch
    -- with the decision, updated with the outcome when the background job ends.
    pipelines     jsonb       NOT NULL DEFAULT '{}'::jsonb,

    -- Set on the row that fired `post_game_incomplete`, so the alert survives a
    -- restart of the process whose in-memory dedupe map would otherwise be the
    -- only thing stopping it repeating every 15 minutes.
    alerted       boolean     NOT NULL DEFAULT false
);

CREATE INDEX IF NOT EXISTS pipeline_batches_category_nba_date
    ON nba.pipeline_batches USING btree (category, nba_date);

CREATE INDEX IF NOT EXISTS pipeline_batches_triggered_at
    ON nba.pipeline_batches USING btree (triggered_at DESC);
