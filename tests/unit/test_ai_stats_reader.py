"""
The connection query_stats runs on (services/ai/stats/reader.py): the
dedicated ask_reader pool when its URL is set, the main pool in a read-only
transaction when it is not. What each path does on a database is in
tests/integration/test_ai_stats_views_integration.py.
"""

import pytest
from peewee import OperationalError

from core.settings import Settings
from services.ai.stats import reader as reader_module
from services.ai.stats.reader import StatsReader, StatsQueryTimeout, _sqlstate


@pytest.mark.unit
def test_the_setting_is_off_by_default():
    assert Settings.model_fields["ask_reader_database_url"].default is None


@pytest.mark.unit
def test_without_a_url_the_reader_uses_the_main_pool():
    r = StatsReader(None)
    assert r.mode == "main_pool"
    assert r.pool is None


@pytest.mark.unit
def test_with_a_url_the_reader_has_a_small_bounded_pool():
    r = StatsReader("postgresql://ask_reader:secret@db.example:5432/cv")
    assert r.mode == "ask_reader"
    pool = r.pool
    assert pool is not None
    assert pool.database == "cv"
    assert pool._max_connections == reader_module.MAX_CONNECTIONS == 4
    params = pool.connect_params
    assert params["user"] == "ask_reader"
    assert params["connect_timeout"] == 5 and params["keepalives"] == 1
    assert f"statement_timeout={reader_module.STATEMENT_TIMEOUT_MS}" in params["options"]
    assert "default_transaction_read_only=on" in params["options"]
    assert reader_module.STATEMENT_TIMEOUT_MS == 2000


@pytest.mark.unit
def test_a_statement_timeout_is_read_from_the_drivers_sqlstate():
    class Driver(Exception):
        pgcode = "57014"

    inner = Driver("canceling statement due to statement timeout")
    # Peewee re-raises the driver's error as its own, with the original as args[0].
    assert _sqlstate(OperationalError(inner, *inner.args)) == "57014"
    assert _sqlstate(OperationalError("no code")) is None


@pytest.mark.unit
def test_the_timeout_error_tells_the_model_how_to_narrow():
    err = StatsQueryTimeout()
    assert err.error_code == "STATS_QUERY_TIMEOUT"
    assert "2 s" in err.message and "fewer players" in err.message


@pytest.mark.unit
def test_the_process_reader_is_built_once(monkeypatch):
    monkeypatch.setattr(reader_module, "_reader", None)
    first = reader_module.get_reader()
    assert reader_module.get_reader() is first
    assert first.mode == "main_pool"  # the suite sets no ASK_READER_DATABASE_URL
