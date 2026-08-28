"""PostgreSQL checks for the bounded Peewee worker boundary."""

from __future__ import annotations

import asyncio
import threading

import pytest

from core.settings import settings
from db import base as db_base
from db.models.users import User


@pytest.mark.integration
@pytest.mark.asyncio
async def test_worker_connections_transactions_materialization_and_pool_bound(integration_db):
    db_base.start_db_runtime()
    main_thread = threading.get_ident()
    baseline_in_use = len(integration_db._in_use)

    def insert_and_materialize():
        User.create(email="worker@courtvision.dev", clerk_user_id="worker-user")
        return [
            {"user_id": row.user_id, "email": row.email}
            for row in User.select().where(User.clerk_user_id == "worker-user")
        ], threading.get_ident()

    rows, worker_thread = await db_base.run_db("integration.materialize", insert_and_materialize)
    assert rows == [{"user_id": rows[0]["user_id"], "email": "worker@courtvision.dev"}]
    assert worker_thread != main_thread
    assert all(isinstance(row, dict) for row in rows)
    assert len(integration_db._in_use) == baseline_in_use

    def rolled_back_write():
        with integration_db.atomic():
            User.create(email="rollback@courtvision.dev", clerk_user_id="rollback-user")
            raise RuntimeError("force rollback")

    with pytest.raises(RuntimeError, match="force rollback"):
        await db_base.run_db("integration.rollback", rolled_back_write)

    count = await db_base.run_db(
        "integration.rollback_count",
        lambda: User.select().where(User.clerk_user_id == "rollback-user").count(),
    )
    assert count == 0

    active = maximum = 0
    lock = threading.Lock()

    def slow_query():
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        try:
            integration_db.execute_sql("SELECT pg_sleep(0.03)").fetchone()
        finally:
            with lock:
                active -= 1

    await asyncio.gather(*(
        db_base.run_db("integration.pool_cap", slow_query)
        for _ in range(settings.db_max_in_flight * 2)
    ))
    assert maximum <= settings.db_max_in_flight
    assert len(integration_db._in_use) == baseline_in_use

