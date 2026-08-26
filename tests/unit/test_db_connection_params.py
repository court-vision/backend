"""Pooled connections carry bounded socket waits (keepalives, statement_timeout)."""

import pytest

from db.base import db


@pytest.mark.unit
def test_connect_params_bound_socket_waits():
    params = db.connect_params
    assert params["connect_timeout"] == 10
    assert params["keepalives"] == 1 and params["keepalives_count"] == 3
    assert "statement_timeout=30000" in params["options"]
    assert db._stale_timeout == 300
