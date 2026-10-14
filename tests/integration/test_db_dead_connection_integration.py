"""
Pooled connections whose server went away (db.base._use_live_connection).

Pooled connections live an hour, and Peewee's pool checks only libpq's local
state at checkout, so after a Postgres restart or failover every idle pooled
connection is dead but looks fine, and each one would fail the next request
that draws it. run_db pings a connection that has sat idle and swaps a dead one
for a live one. pg_terminate_backend is what a restart's fast shutdown does to
each connection: the server says goodbye and closes the socket.
"""

from __future__ import annotations

import asyncio
import time

import pytest
import pytest_asyncio
from peewee import OperationalError

from core.settings import settings
from db import base as db_base


def _backend_pid(db) -> int:
    return db.execute_sql("SELECT pg_backend_pid()").fetchone()[0]


def _terminate(db, pids) -> None:
    for pid in pids:
        db.execute_sql("SELECT pg_terminate_backend(%s)", (pid,))
    time.sleep(0.1)   # the backends exit asynchronously


@pytest_asyncio.fixture
async def cold_pool(integration_db):
    """A pool with nothing in it, and the main thread's connection reopened for the test's own use."""
    await db_base.stop_db_runtime()
    integration_db.connect(reuse_if_open=True)
    yield integration_db
    await db_base.stop_db_runtime()
    integration_db.connect(reuse_if_open=True)


@pytest.fixture
def pings(integration_db, monkeypatch):
    """Counts the liveness pings run_db makes."""
    count = []
    execute_sql = integration_db.execute_sql

    def counting(sql, params=None, *args, **kwargs):
        if sql == "SELECT 1":
            count.append(sql)
        return execute_sql(sql, params, *args, **kwargs)
    monkeypatch.setattr(integration_db, "execute_sql", counting)
    return count


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_idle_connection_whose_server_went_away_is_replaced(cold_pool, monkeypatch):
    monkeypatch.setattr(settings, "db_pool_ping_after_idle_seconds", 0.2)
    first = await db_base.run_db("test.pid", _backend_pid, cold_pool)

    _terminate(cold_pool, [first])
    await asyncio.sleep(0.3)   # idle past the threshold

    second = await db_base.run_db("test.pid", _backend_pid, cold_pool)
    assert second != first


@pytest.mark.integration
@pytest.mark.asyncio
async def test_without_the_ping_the_dead_connection_fails_its_request(cold_pool, monkeypatch):
    """What every request drawing a dead connection got before: an error, then a 503."""
    monkeypatch.setattr(settings, "db_pool_ping_after_idle_seconds", 3600)
    first = await db_base.run_db("test.pid", _backend_pid, cold_pool)
    _terminate(cold_pool, [first])

    with pytest.raises(OperationalError):
        await db_base.run_db("test.pid", _backend_pid, cold_pool)
    # The pool drops the broken connection when it is handed back.
    assert await db_base.run_db("test.pid", _backend_pid, cold_pool) != first


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_burst_after_a_restart_gets_live_connections(cold_pool, monkeypatch):
    """Every warmed connection dead at once, as after a Postgres restart."""
    await db_base.warm_db_pool(4)
    warmed = {row[0] for row in cold_pool.execute_sql(
        "SELECT pid FROM pg_stat_activity WHERE datname = current_database() AND pid <> pg_backend_pid()"
    ).fetchall()}
    assert len(warmed) == 4
    _terminate(cold_pool, warmed)
    monkeypatch.setattr(settings, "db_pool_ping_after_idle_seconds", 0)

    pids = await asyncio.gather(*(db_base.run_db("test.burst", _backend_pid, cold_pool) for _ in range(4)))
    assert not set(pids) & warmed


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_connection_used_moments_ago_is_not_pinged(cold_pool, pings):
    first = await db_base.run_db("test.pid", _backend_pid, cold_pool)
    assert len(pings) == 1   # a connection run_db had not handed out before

    for _ in range(3):
        assert await db_base.run_db("test.pid", _backend_pid, cold_pool) == first
    assert len(pings) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_warmed_connection_is_trusted_like_a_used_one(cold_pool, pings):
    await db_base.warm_db_pool(2)
    await asyncio.gather(*(db_base.run_db("test.burst", _backend_pid, cold_pool) for _ in range(2)))
    assert pings == []
