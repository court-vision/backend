"""
The 90-day redaction of AI question text, away from the insert it usually rides
on (services/ai/questions.py): the sweep the application runs once at startup
(main.lifespan), so the policy holds when nobody is asking anything.

No database: the sweep's SQL is covered in
tests/integration/test_ai_questions_integration.py. What is pinned here is that
startup runs it, and that a sweep which fails or never finishes cannot fail or
hold up a startup.
"""

import asyncio
from types import SimpleNamespace

import pytest

import main
from services.ai import questions

_STARTUP = ("setup_logging", "init_db", "start_db_runtime", "start_cpu_runtime", "start_provider_runtime",
            "start_blocking_provider_runtime", "start_features_runtime", "start_sqlmate_runtime",
            "start_fantasy_writer_runtime", "assert_calendar_available")
_SHUTDOWN = ("stop_features_runtime", "stop_sqlmate_runtime", "stop_fantasy_writer_runtime", "stop_provider_runtime",
             "stop_blocking_provider_runtime", "stop_cpu_runtime", "stop_db_runtime")


@pytest.fixture
def lifespan(monkeypatch):
    """main.lifespan with every other startup and shutdown step stubbed out."""
    async def stopped():
        return None
    for name in _STARTUP + ("close_db",):
        monkeypatch.setattr(main, name, lambda *args, **kwargs: None)
    for name in _SHUTDOWN:
        monkeypatch.setattr(main, name, stopped)
    monkeypatch.setattr(main, "start_loop_watchdog", lambda loop, stall: SimpleNamespace(stop=lambda: None))
    return main.lifespan


@pytest.fixture
def logged(monkeypatch):
    lines = []
    log = SimpleNamespace(info=lambda event, **kw: lines.append((event, kw)),
                          exception=lambda event, **kw: lines.append((event, kw)))
    monkeypatch.setattr(questions, "get_logger", lambda: log)
    return lines


@pytest.mark.unit
def test_startup_runs_the_retention_sweep(lifespan, logged, monkeypatch):
    """It ran only when a question was recorded, so with no AI traffic nothing was ever redacted."""
    async def sweep():
        return 3
    monkeypatch.setattr(questions, "_sweep", sweep)

    async def run():
        async with lifespan(main.app):
            await asyncio.sleep(0.01)
    asyncio.run(run())

    assert logged == [("ai_questions_redacted", {"rows": 3, "trigger": "startup"})]


@pytest.mark.unit
def test_a_failing_sweep_does_not_fail_startup(lifespan, logged, monkeypatch):
    async def sweep():
        raise RuntimeError("database gone")
    monkeypatch.setattr(questions, "_sweep", sweep)
    started = []

    async def run():
        async with lifespan(main.app):
            await asyncio.sleep(0.01)
            started.append(True)
    asyncio.run(run())

    assert started == [True]
    assert logged == [("ai_question_retention_failed", {"trigger": "startup"})]


@pytest.mark.unit
def test_a_sweep_that_never_finishes_does_not_hold_up_startup_or_shutdown(lifespan, logged, monkeypatch):
    state = []

    async def sweep():
        state.append("running")
        try:
            await asyncio.Event().wait()
        finally:
            state.append("cancelled")
    monkeypatch.setattr(questions, "_sweep", sweep)

    async def run():
        async with lifespan(main.app):
            await asyncio.sleep(0.01)
            state.append("serving")
        await asyncio.sleep(0)
    asyncio.run(asyncio.wait_for(run(), timeout=5))

    assert state == ["running", "serving", "cancelled"]
    assert logged == []
