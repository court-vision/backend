"""
Opening pooled DB connections at startup (db.base.warm_db_pool).

After a deploy the pool started empty, and Peewee opens connections one at a
time under a process-wide lock, so the first page open's parallel requests
queued behind each other's connects. The warm-up opens them before traffic.
Each worker must hold its connection until every worker has one, or the pool
would hand the first connection to the next worker and end up holding one.
It is best effort: nothing here may fail or hang a startup.

No database here: `db.connect`/`db.close` are replaced. The real pool is
exercised in tests/integration/test_db_pool_warmup_integration.py.
"""

from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
import pytest_asyncio
from peewee import OperationalError

from core.settings import settings
from db import base as db_base


@pytest_asyncio.fixture(autouse=True)
async def isolated_db_runtime(monkeypatch):
    await db_base.stop_db_runtime()
    monkeypatch.setattr(db_base.db, "close_all", lambda: None)
    yield
    await db_base.stop_db_runtime()


@pytest.fixture
def logged(monkeypatch):
    lines = []
    monkeypatch.setattr(db_base, "log", SimpleNamespace(
        info=lambda event, **kw: lines.append((event, kw)),
        warning=lambda event, **kw: lines.append((event, kw)),
    ))
    return lines


class FakeConnections:
    """Stands in for db.connect/db.close, tracking how many are held at once."""

    def __init__(self, fail_on: set[int] = frozenset()):
        self.fail_on = fail_on
        self.lock = threading.Lock()
        self.connects = 0
        self.held = 0
        self.most_held = 0

    def connect(self, reuse_if_open=False):
        with self.lock:
            self.connects += 1
            n = self.connects
        time.sleep(0.005)
        if n in self.fail_on:
            raise OperationalError("could not connect to server")
        with self.lock:
            self.held += 1
            self.most_held = max(self.most_held, self.held)
        return True

    def close(self):
        with self.lock:
            self.held -= 1
        return True


@pytest.fixture
def connections(monkeypatch):
    def install(**kwargs):
        fake = FakeConnections(**kwargs)
        monkeypatch.setattr(db_base.db, "connect", fake.connect)
        monkeypatch.setattr(db_base.db, "close", fake.close)
        return fake
    return install


@pytest.mark.unit
@pytest.mark.asyncio
async def test_every_connection_is_held_until_all_are_open(connections, logged):
    fake = connections()
    await db_base.warm_db_pool(6)
    assert fake.connects == 6
    assert fake.most_held == 6   # six distinct connections, not one handed round
    assert fake.held == 0        # all back in the pool
    event, fields = logged[-1]
    assert event == "db_pool_warmed"
    assert fields["connections"] == 6
    assert fields["connect_ms"] >= 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_never_more_than_the_db_worker_budget(connections, logged, monkeypatch):
    monkeypatch.setattr(settings, "db_max_in_flight", 3)
    fake = connections()
    await db_base.warm_db_pool(10)
    assert fake.connects == 3
    assert logged[-1][1]["connections"] == 3


@pytest.mark.unit
@pytest.mark.asyncio
async def test_zero_turns_it_off(connections, logged):
    fake = connections()
    await db_base.warm_db_pool(0)
    assert fake.connects == 0
    assert logged == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_a_failed_connect_is_logged_not_raised_and_does_not_hold_up_startup(connections, logged):
    fake = connections(fail_on={2})
    started = time.perf_counter()
    await db_base.warm_db_pool(4)   # does not raise
    assert time.perf_counter() - started < db_base.WARM_POOL_WAIT_S / 2  # the others stopped waiting
    assert fake.held == 0
    event, fields = logged[-1]
    assert event == "db_pool_warm_failed"
    assert fields["error"] == "OperationalError"   # the cause, not the workers that saw the barrier break
    assert fields["failed"] == 4


@pytest.mark.unit
@pytest.mark.asyncio
async def test_the_db_worker_budget_is_returned(connections, logged, monkeypatch):
    monkeypatch.setattr(settings, "db_max_in_flight", 4)
    connections()
    await db_base.warm_db_pool(4)
    await asyncio.sleep(0)   # permits are released by done callbacks on the loop
    _, capacity = db_base._ensure_db_runtime()
    assert capacity._value == 4
