-- Migration 0033: indexes for "the newest run of this pipeline".
--
-- data.courtvision.dev's /v1/dashboard/status asks nba.pipeline_runs, for
-- every pipeline on each 30 s poll, for its newest run, its newest success and
-- its last ten runs (data-platform api/v1/dashboard.py _build_pipeline_health;
-- the pipeline page and PipelineRun.get_latest_successful ask the same). The
-- table only had single-column indexes on pipeline_name and on status, so for
-- live_game_stats, which owns 77k of the 80k rows (2026-10-13), each of those
-- read the whole table and sorted it: 25-42 ms apiece in production (EXPLAIN
-- ANALYZE), and 17 ms for the quality check that asks whether it succeeded in
-- the last 24 hours. In season live_game_stats adds about 31k rows a month,
-- and a seq scan grows with them.
--
-- (pipeline_name, started_at DESC) answers newest run, last ten, the runs page,
-- the 24-hour checks, was_successful_on_date and count_since as a short range
-- of one pipeline's entries. Newest success orders by completed_at, so it gets
-- its own index over the success rows only; DESC puts NULLs first, as the
-- query's ORDER BY ... DESC does, so the scan needs no sort.
--
-- The baseline carries each single-column index twice, idx_pipeline_runs_* and
-- Peewee's pipelinerun_*, with identical definitions. On 2026-10-13 production's
-- pg_stat_user_indexes showed 0 scans for the idx_* pair and 5,884 and 1,969
-- for pipelinerun_* (the planner takes those), so the idx_* pair goes and no
-- plan that uses an index today changes. pipelinerun_status still serves the
-- status = 'running' sweeps, and both are what the models' index=True describe.
--
-- Plain CREATE INDEX, not CONCURRENTLY: on a 15 MB table the build holds its
-- lock (writes wait, reads do not) for well under a second, and one transaction
-- applies the whole file or none of it. CONCURRENTLY can leave an invalid index
-- behind that IF NOT EXISTS would then skip for good. The drops come last so
-- their ACCESS EXCLUSIVE lock is held only for the moment before commit.
--
-- lock_timeout: migrations run at backend startup, before the API serves a
-- request. Without it, a lock request queued behind a long transaction on this
-- table (a write caught in one of prod's commit stalls, say) would hold that
-- startup for as long as the transaction lasts, and every pipeline's start_run
-- insert would queue behind the request. With it the migration gives up after
-- 5 s with nothing applied, the startup fails, and the next start tries again.
SET LOCAL lock_timeout = '5s';

CREATE INDEX IF NOT EXISTS pipeline_runs_name_started
    ON nba.pipeline_runs USING btree (pipeline_name, started_at DESC);

CREATE INDEX IF NOT EXISTS pipeline_runs_name_completed_success
    ON nba.pipeline_runs USING btree (pipeline_name, completed_at DESC)
    WHERE status = 'success';

DROP INDEX IF EXISTS nba.idx_pipeline_runs_pipeline_name;
DROP INDEX IF EXISTS nba.idx_pipeline_runs_status;
