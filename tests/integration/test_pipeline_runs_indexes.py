"""
Integration: migration 0033's indexes on nba.pipeline_runs.

data-platform's ops dashboard reads "the newest run of this pipeline" from this
table for every pipeline on each 30 s poll, and before 0033 the busiest pipeline
(live_game_stats, 77k of prod's 80k rows) answered each read with a seq scan and
a sort. The reads below are built from this repo's PipelineRun model in the
shapes data-platform builds them (api/v1/dashboard.py _build_pipeline_health and
_build_runs, and its db/models/pipeline_run.py), so Peewee renders the same SQL;
their plans must read an index range with no sort. Also: only the duplicates
production never scanned are dropped, the rollback goes through yoyo and back,
and a migration that cannot get its lock fails in seconds with nothing applied
instead of queueing every pipeline's start_run behind it.
"""

import os
import threading
import time
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest
from peewee import SQL
from yoyo import get_backend, read_migrations

from db.base import db
from db.migrate import MIGRATIONS_DIR
from db.models.pipeline_run import PipelineRun

pytestmark = [pytest.mark.integration]

MIGRATION_ID = "0033__pipeline_runs_latest_run_indexes"
BUSY = "live_game_stats"
SMALL = "team_stats"

BASELINE_INDEXES = {
    "pipeline_runs_pkey",
    "idx_pipeline_runs_pipeline_name",
    "idx_pipeline_runs_status",
    "pipelinerun_pipeline_name",
    "pipelinerun_status",
}
INDEXES_0033 = {
    "pipeline_runs_pkey",
    "pipelinerun_pipeline_name",
    "pipelinerun_status",
    "pipeline_runs_name_started",
    "pipeline_runs_name_completed_success",
}

# 20k runs of the busy pipeline six minutes apart (every 131st failed), a small
# daily pipeline, and one run in flight: enough rows that a seq scan and sort is
# what the planner picks without the indexes.
LOAD = """
INSERT INTO nba.pipeline_runs (id, pipeline_name, started_at, completed_at, status, records_processed)
SELECT gen_random_uuid(), %(busy)s, now() - i * interval '6 minutes',
       CASE WHEN i %% 131 = 0 THEN NULL ELSE now() - i * interval '6 minutes' + interval '4 seconds' END,
       CASE WHEN i %% 131 = 0 THEN 'failed' ELSE 'success' END, 200
FROM generate_series(1, 20000) i;

INSERT INTO nba.pipeline_runs (id, pipeline_name, started_at, completed_at, status, records_processed)
SELECT gen_random_uuid(), %(small)s, now() - i * interval '1 day',
       now() - i * interval '1 day' + interval '40 seconds',
       CASE WHEN i %% 3 = 0 THEN 'failed' ELSE 'success' END, 500
FROM generate_series(1, 84) i;

INSERT INTO nba.pipeline_runs (id, pipeline_name, started_at, status)
VALUES (gen_random_uuid(), %(busy)s, now() - interval '20 seconds', 'running');
"""


def _indexes() -> dict[str, str]:
    rows = db.execute_sql(
        "SELECT indexname, indexdef FROM pg_indexes WHERE schemaname = 'nba' AND tablename = 'pipeline_runs'"
    ).fetchall()
    return dict(rows)


def _nodes(plan: dict):
    yield plan
    for child in plan.get("Plans", []):
        yield from _nodes(child)


def _plan(query) -> list[dict]:
    sql, params = query.sql()
    plan = db.execute_sql("EXPLAIN (FORMAT JSON) " + sql, params).fetchone()[0][0]["Plan"]
    return list(_nodes(plan))


def _index_read(query) -> str:
    """The index a plan reads, asserting it neither seq-scans nor sorts."""
    nodes = _plan(query)
    types = [n["Node Type"] for n in nodes]
    assert "Seq Scan" not in types and "Sort" not in types, types
    (name,) = [n["Index Name"] for n in nodes if "Index Name" in n]
    return name


def _applied() -> bool:
    row = db.execute_sql(
        "SELECT 1 FROM public._yoyo_migration WHERE migration_id = %s", (MIGRATION_ID,)
    ).fetchone()
    return row is not None


def _yoyo(action: str) -> None:
    """Apply or roll back 0033 the way startup does: through yoyo, under its lock.
    A no-op when it is already in that state."""
    migrations = read_migrations(str(MIGRATIONS_DIR)).filter(lambda m: m.id == MIGRATION_ID)
    with get_backend(os.environ["DATABASE_URL"]) as backend:
        with backend.lock():
            if action == "apply":
                backend.apply_migrations(backend.to_apply(migrations))
            else:
                backend.rollback_migrations(backend.to_rollback(migrations))


@pytest.fixture
def busy_runs(integration_db):
    """The synthetic runs, rolled back after the test; ANALYZE inside counts them."""
    with db.atomic() as txn:
        db.execute_sql(LOAD, {"busy": BUSY, "small": SMALL})
        db.execute_sql("ANALYZE nba.pipeline_runs")
        yield
        txn.rollback()


def test_0033_is_applied_by_the_chain_and_drops_only_the_unused_duplicates(integration_db):
    assert _applied()
    indexes = _indexes()
    assert set(indexes) == INDEXES_0033
    assert indexes["pipeline_runs_name_started"].endswith("USING btree (pipeline_name, started_at DESC)")
    assert indexes["pipeline_runs_name_completed_success"].endswith(
        "USING btree (pipeline_name, completed_at DESC) WHERE ((status)::text = 'success'::text)"
    )


def _reads(name: str, since: datetime) -> dict[str, object]:
    P = PipelineRun
    return {
        # _build_pipeline_health: newest run, newest success, the error streak's last ten
        "latest_run": P.select().where(P.pipeline_name == name).order_by(P.started_at.desc()).limit(1),
        "latest_success": (
            P.select()
            .where((P.pipeline_name == name) & (P.status == "success"))
            .order_by(P.completed_at.desc())
            .limit(1)
        ),
        "last_ten": P.select(P.status).where(P.pipeline_name == name).order_by(P.started_at.desc()).limit(10),
        # _build_runs: the pipeline page
        "runs_page": P.select().where(P.pipeline_name == name).order_by(P.started_at.desc()).limit(50),
        # the gates: was_successful_on_date, count_since
        "succeeded_since": (
            P.select(SQL("1"))
            .where((P.pipeline_name == name) & (P.status == "success") & (P.started_at >= since))
            .limit(1)
        ),
        "started_since": P.select(SQL("1")).where((P.pipeline_name == name) & (P.started_at >= since)),
    }


def test_the_busy_pipelines_newest_run_reads_are_index_ranges(busy_runs):
    reads = _reads(BUSY, datetime.now(timezone.utc) - timedelta(days=1))
    assert {label: _index_read(q) for label, q in reads.items()} == {
        "latest_run": "pipeline_runs_name_started",
        "latest_success": "pipeline_runs_name_completed_success",
        "last_ten": "pipeline_runs_name_started",
        "runs_page": "pipeline_runs_name_started",
        "succeeded_since": "pipeline_runs_name_started",
        "started_since": "pipeline_runs_name_started",
    }


def test_a_small_pipelines_reads_never_scan_the_table(busy_runs):
    # With 84 rows the planner may as well read pipelinerun_pipeline_name and
    # sort them (both are well under a millisecond); what matters is that the
    # busy pipeline's 20k rows are never read to find one of them.
    for label, query in _reads(SMALL, datetime.now(timezone.utc) - timedelta(days=1)).items():
        types = [n["Node Type"] for n in _plan(query)]
        assert "Seq Scan" not in types, (label, types)


def test_the_newest_success_is_still_the_right_row(busy_runs):
    # The index plan returns what a full sort would. ORDER BY completed_at DESC
    # puts NULLs first, and the failed and running rows here have none, but the
    # partial index holds success rows only, and mark_success always sets it.
    P = PipelineRun
    newest = P.get_latest_successful(BUSY)
    assert newest.status == "success"
    assert newest.completed_at == (
        P.select(P.completed_at)
        .where((P.pipeline_name == BUSY) & (P.status == "success"))
        .order_by(P.completed_at.desc(nulls="LAST"))
        .scalar()
    )


def test_0033_rollback_restores_the_baseline_and_reapplies(integration_db):
    try:
        _yoyo("rollback")
        assert not _applied()
        assert set(_indexes()) == BASELINE_INDEXES
    finally:
        _yoyo("apply")                                                  # the chain's state is untouched
    assert _applied()
    assert set(_indexes()) == INDEXES_0033
    db.execute_sql((MIGRATIONS_DIR / f"{MIGRATION_ID}.sql").read_text())  # idempotent: a second run is a no-op
    assert set(_indexes()) == INDEXES_0033


def test_0033_gives_up_on_a_held_lock_with_nothing_applied(integration_db):
    _yoyo("rollback")
    holder = psycopg2.connect(os.environ["DATABASE_URL"])
    # Should lock_timeout ever go missing, the apply below would wait on the
    # holder forever; let go after 20 s so the test fails instead of hanging.
    release = threading.Timer(20, holder.rollback)
    try:
        with holder.cursor() as cur:
            # What an open write transaction on the table holds, e.g. a start_run
            # insert caught in a commit stall. The index build needs SHARE.
            cur.execute("LOCK TABLE nba.pipeline_runs IN ROW EXCLUSIVE MODE")
        release.start()
        started = time.monotonic()
        with pytest.raises(psycopg2.errors.LockNotAvailable):
            _yoyo("apply")
        assert time.monotonic() - started < 15                          # lock_timeout is 5 s
        assert not _applied()
        assert set(_indexes()) == BASELINE_INDEXES
    finally:
        release.cancel()
        holder.rollback()
        holder.close()
        _yoyo("apply")
    assert _applied() and set(_indexes()) == INDEXES_0033
