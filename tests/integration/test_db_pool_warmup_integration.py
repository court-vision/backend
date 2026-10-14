"""
The startup pool warm-up against Postgres: it leaves distinct, idle pooled
connections behind, and the first burst of parallel DB work uses them instead
of opening its own one at a time (see tests/unit/test_db_pool_warmup.py).
"""

from __future__ import annotations

import asyncio

import pytest

from core.settings import settings
from db import base as db_base


def _backend_pids(db) -> set[int]:
    return {row[0] for row in db.execute_sql(
        "SELECT pid FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
    ).fetchall()}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_warm_up_leaves_idle_connections_the_first_burst_reuses(integration_db):
    await db_base.stop_db_runtime()   # closes the pool: start from a cold process
    integration_db.connect(reuse_if_open=True)
    try:
        before = _backend_pids(integration_db)
        await db_base.warm_db_pool(4)
        warmed = _backend_pids(integration_db) - before
        assert len(warmed) == 4

        def backend_pid():
            return integration_db.execute_sql("SELECT pg_backend_pid()").fetchone()[0]

        # Four parallel operations, the shape of a page open's first wave.
        pids = await asyncio.gather(*(db_base.run_db("test.burst", backend_pid) for _ in range(4)))
        assert set(pids) <= warmed
        assert _backend_pids(integration_db) - before == warmed   # nothing new was opened
    finally:
        await db_base.stop_db_runtime()
        integration_db.connect(reuse_if_open=True)


@pytest.mark.integration
def test_pool_recycles_by_the_configured_age(integration_db):
    assert integration_db._stale_timeout == settings.db_pool_stale_timeout_seconds == 3600
