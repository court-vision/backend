"""Pooled connections carry bounded socket waits (keepalives, statement_timeout)."""

import pytest
from pydantic import ValidationError

from core.settings import Settings, settings
from db.base import db


@pytest.mark.unit
def test_connect_params_bound_socket_waits():
    params = db.connect_params
    assert params["connect_timeout"] == 10
    assert params["keepalives"] == 1 and params["keepalives_count"] == 3
    assert "statement_timeout=30000" in params["options"]


@pytest.mark.unit
def test_pooled_connections_live_an_hour():
    """Peewee closes a pooled connection this long after it was opened, and
    reconnects one at a time under a process-wide lock. At 300 s every page
    open after five quiet minutes queued its requests behind ~11 serial
    reconnects, about 90-330 ms on each request in prod."""
    assert Settings.model_fields["db_pool_stale_timeout_seconds"].default == 3600
    assert db._stale_timeout == settings.db_pool_stale_timeout_seconds


@pytest.mark.unit
def test_startup_opens_a_few_pooled_connections_by_default():
    assert Settings.model_fields["db_pool_warm_connections"].default == 8
    assert settings.db_pool_warm_connections <= settings.db_max_in_flight


@pytest.mark.unit
def test_connections_idle_half_a_minute_are_pinged_before_use():
    """An hour's lifetime means a Postgres restart leaves dead connections in
    the pool, which Peewee's checkout cannot tell from live ones."""
    assert Settings.model_fields["db_pool_ping_after_idle_seconds"].default == 30.0


@pytest.mark.unit
@pytest.mark.parametrize("field,value", [
    ("db_pool_stale_timeout_seconds", 0),    # Peewee would never recycle a connection
    ("db_pool_stale_timeout_seconds", -1),
    ("db_pool_warm_connections", -1),
    ("db_pool_ping_after_idle_seconds", -1),
])
def test_pool_settings_refuse_nonsense(field, value):
    with pytest.raises(ValidationError, match=field):
        Settings(**{field: value})
