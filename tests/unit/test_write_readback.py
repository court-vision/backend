"""
services.write_readback: a write chain reads the board again, a little later each
time, while ESPN's read still shows the board from before the write.
"""

import asyncio

import pytest

from core.errors import ProviderTimeout
from services import write_readback


def board(version):
    """`read_until_shown` hands boards through untouched; a stand-in names which read it was."""
    return {"version": version}


def reader(*answers):
    seen = []
    queue = list(answers)

    async def read():
        seen.append(len(seen) + 1)
        nxt = queue.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    return read, seen


@pytest.fixture
def slept(monkeypatch):
    naps = []

    async def nap(seconds):
        naps.append(seconds)

    monkeypatch.setattr(write_readback.asyncio, "sleep", nap)
    return naps


def run(read, shows, **kw):
    return asyncio.run(write_readback.read_until_shown(read, shows, **kw))


@pytest.mark.unit
def test_a_write_that_shows_at_once_is_read_once(slept):
    read, seen = reader(board("v2"))
    assert run(read, lambda b: b["version"] == "v2", delays=(0.5, 1.0)) == (board("v2"), True, 1)
    assert seen == [1] and slept == []


@pytest.mark.unit
def test_a_trailing_read_is_asked_again_until_it_shows(slept):
    read, seen = reader(board("v1"), board("v1"), board("v2"))
    assert run(read, lambda b: b["version"] == "v2", delays=(0.5, 1.0, 1.5)) == (board("v2"), True, 3)
    assert slept == [0.5, 1.0]   # the third delay is never slept


@pytest.mark.unit
def test_a_write_that_never_shows_comes_back_unconfirmed_with_the_last_read(slept):
    read, seen = reader(board("v1"), board("v1b"), board("v1c"))
    assert run(read, lambda b: False, delays=(0.5, 1.0)) == (board("v1c"), False, 3)
    assert slept == [0.5, 1.0]


@pytest.mark.unit
def test_a_failed_reread_ends_the_wait_with_the_board_in_hand(slept):
    read, seen = reader(board("v1"), ProviderTimeout("espn"), board("v2"))
    assert run(read, lambda b: b["version"] == "v2", delays=(0.5, 1.0)) == (board("v1"), False, 1)
    assert seen == [1, 2]


@pytest.mark.unit
def test_the_first_reads_failure_is_the_callers(slept):
    read, _ = reader(ProviderTimeout("espn"))
    with pytest.raises(ProviderTimeout):
        run(read, lambda b: True, delays=(0.5,))


@pytest.mark.unit
def test_the_default_wait_is_five_seconds_at_most(slept, monkeypatch):
    monkeypatch.setattr(write_readback, "READBACK_DELAYS_S", (0.5, 1.0, 1.5, 2.0))
    read, seen = reader(*[board("v1")] * 5)
    assert run(read, lambda b: False)[1:] == (False, 5)
    assert sum(slept) == 5.0
