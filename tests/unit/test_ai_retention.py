"""
The 90-day redaction of AI question text, away from the insert it usually rides
on (services/ai/questions.py): the sweep the application runs at startup and
then daily while it is up (main.lifespan), so the policy holds when nobody is
asking anything and nothing is being deployed.

No database: the sweep's SQL is covered in
tests/integration/test_ai_questions_integration.py. What is pinned here is that
startup runs it and keeps running it, and that a sweep which fails or never
finishes cannot fail or hold up a startup.
"""

import asyncio
from datetime import timedelta
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


def _sweeps(*results):
    """A stand-in for the sweep that returns (or raises) each of `results` in turn,
    then never finishes -- which stops a loop with no wait between sweeps."""
    remaining = list(results)

    async def sweep():
        if not remaining:
            await asyncio.Event().wait()
        result = remaining.pop(0)
        if isinstance(result, Exception):
            raise result
        return result
    return sweep


@pytest.mark.unit
def test_the_sweep_runs_again_while_the_application_stays_up(lifespan, logged, monkeypatch):
    """Once at startup left retention waiting on the next deploy: a process that
    stayed up with no questions asked kept text past 90 days."""
    monkeypatch.setattr(questions, "SWEEP_EVERY", timedelta(0))
    monkeypatch.setattr(questions, "_sweep", _sweeps(3, 1))

    async def run():
        async with lifespan(main.app):
            await asyncio.sleep(0.01)
    asyncio.run(asyncio.wait_for(run(), timeout=5))

    assert logged == [("ai_questions_redacted", {"rows": 3, "trigger": "startup"}),
                      ("ai_questions_redacted", {"rows": 1, "trigger": "daily"})]


@pytest.mark.unit
def test_a_failing_sweep_is_tried_again_the_next_day(lifespan, logged, monkeypatch):
    monkeypatch.setattr(questions, "SWEEP_EVERY", timedelta(0))
    monkeypatch.setattr(questions, "_sweep", _sweeps(RuntimeError("database gone"), 2))

    async def run():
        async with lifespan(main.app):
            await asyncio.sleep(0.01)
    asyncio.run(asyncio.wait_for(run(), timeout=5))

    assert logged == [("ai_question_retention_failed", {"trigger": "startup"}),
                      ("ai_questions_redacted", {"rows": 2, "trigger": "daily"})]


@pytest.mark.unit
def test_the_sweep_waits_a_day_between_runs(monkeypatch):
    waits = []

    async def sweep():
        return 0

    async def sleep(seconds):
        waits.append(seconds)
        if len(waits) == 2:
            raise asyncio.CancelledError
    monkeypatch.setattr(questions, "_sweep", sweep)
    monkeypatch.setattr(questions, "get_logger", lambda: SimpleNamespace(info=lambda event, **kw: None))
    monkeypatch.setattr(questions.asyncio, "sleep", sleep)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(questions.keep_redacting())

    assert waits == [86400.0, 86400.0]


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
