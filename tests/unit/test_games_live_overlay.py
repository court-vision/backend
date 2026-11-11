"""
Where today's slate (GET /v1/games/{today}) gets each game's live score, status,
period and clock.

They were read from the NBA's scoreboard file on every request, and that file
moves about once in ten minutes (the median gap between changes was 600 s in
every game of 2026-11-09): the slate still showed a tip time for minutes after
the tip and held each score for ten. They are now the live pipeline's newest
snapshot of the game, which is its live box score within the last 30 seconds.

The scoreboard is still asked when the snapshots cannot answer (none yet, or
one that has stopped moving for a game not final), and then each game takes
whichever of the two is further along.

Nothing here touches the database or the network: the stored games, their
snapshots and the scoreboard are handed in. The query behind the snapshots is
tests/integration/test_games_live_overlay_integration.py.
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest
from freezegun import freeze_time

from db.models.nba.live_game_score_snapshots import LatestSnapshot
from schemas.common import ApiStatus
from services import games_service
from services.games_service import GamesService, SNAPSHOT_FRESH_SECONDS, live_fields, needs_scoreboard

pytestmark = pytest.mark.unit

TODAY = date(2026, 11, 10)
NOW = datetime(2026, 11, 11, 1, 30, 0)      # 8:30 PM ET, UTC and naive like `captured_at`
KNICKS, LAKERS, SUNS = "0022600151", "0022600152", "0022600153"


def stored(game_id: str, home: str = "NYK", away: str = "BOS") -> dict:
    """A game as nba.games has it before the post-game run: scheduled, no score."""
    return {
        "game_id": game_id, "game_date": TODAY.isoformat(), "home_team": home, "away_team": away,
        "home_score": None, "away_score": None, "status": "scheduled", "arena": "Madison Square Garden",
        "period": None, "game_clock": None, "start_time_et": "19:30",
    }


def snapshot(status=2, period=2, clock="PT04M41.00S", home=49, away=51, age=20) -> LatestSnapshot:
    return LatestSnapshot(status, period, clock, home, away, NOW - timedelta(seconds=age))


def listed(game_id: str, status=2, period=1, clock="PT00M00.00S", home=28, away=25) -> dict:
    """The game's entry on the NBA scoreboard, as the extractor hands it over."""
    return {
        "game_id": game_id, "game_status": status, "period": period, "game_clock": clock,
        "home_team": "NYK", "away_team": "BOS", "home_score": home, "away_score": away,
    }


# ---------------------------------------------------------------------------
# needs_scoreboard
# ---------------------------------------------------------------------------


def test_fresh_snapshots_of_every_game_need_no_scoreboard() -> None:
    snapshots = {KNICKS: snapshot(), LAKERS: snapshot(status=1, period=None, clock=None, home=0, away=0)}
    assert not needs_scoreboard([KNICKS, LAKERS], snapshots, NOW)


def test_a_game_with_no_snapshot_needs_the_scoreboard() -> None:
    """Before the pipeline's window opens (15 minutes ahead of the first tip) there are none at all."""
    assert needs_scoreboard([KNICKS, LAKERS], {KNICKS: snapshot()}, NOW)
    assert needs_scoreboard([KNICKS], {}, NOW)
    assert not needs_scoreboard([], {}, NOW)


@pytest.mark.parametrize(("status", "age", "needed"), [
    (2, SNAPSHOT_FRESH_SECONDS, False),
    (2, SNAPSHOT_FRESH_SECONDS + 1, True),     # six polls without one: the pipeline is not running
    (1, SNAPSHOT_FRESH_SECONDS + 1, True),
    (3, 6 * 3600, False),                       # a final does not age: the game is no longer read
])
def test_a_snapshot_that_stopped_moving_needs_the_scoreboard_unless_the_game_is_final(status, age, needed) -> None:
    assert needs_scoreboard([KNICKS], {KNICKS: snapshot(status=status, age=age)}, NOW) is needed


# ---------------------------------------------------------------------------
# live_fields
# ---------------------------------------------------------------------------


def test_the_snapshot_is_the_games_live_state() -> None:
    assert live_fields(stored(KNICKS), snapshot(), None) == {
        "home_score": 49, "away_score": 51, "status": "in_progress", "period": 2, "game_clock": "PT04M41.00S",
    }


def test_a_final_keeps_its_period_for_overtime() -> None:
    """The slate prints "Final/OT" off the period."""
    final = live_fields(stored(KNICKS), snapshot(status=3, period=5, clock=None, home=121, away=118), None)
    assert (final["status"], final["period"], final["game_clock"]) == ("final", 5, None)


def test_the_snapshot_stands_against_an_older_scoreboard() -> None:
    """The usual case when both are read: the scoreboard is the one minutes behind."""
    fields = live_fields(stored(KNICKS), snapshot(), listed(KNICKS))
    assert (fields["period"], fields["home_score"], fields["away_score"]) == (2, 49, 51)


@pytest.mark.parametrize("ahead", [
    dict(status=3, period=2, home=49, away=51),      # a later status
    dict(status=2, period=3, home=49, away=51),      # a later period
    dict(status=2, period=2, home=52, away=51),      # more points on the board
])
def test_a_scoreboard_further_along_than_a_stalled_snapshot_is_taken(ahead) -> None:
    fields = live_fields(stored(KNICKS), snapshot(age=900), listed(KNICKS, clock="PT09M00.00S", **ahead))
    assert (fields["status"], fields["period"], fields["home_score"]) == (
        {2: "in_progress", 3: "final"}[ahead["status"]], ahead["period"], ahead["home"],
    )
    assert fields["game_clock"] == "PT09M00.00S"


def test_with_no_snapshot_the_scoreboard_is_the_games_state_as_it_was() -> None:
    assert live_fields(stored(KNICKS), None, listed(KNICKS)) == {
        "home_score": 28, "away_score": 25, "status": "in_progress", "period": 1, "game_clock": "PT00M00.00S",
    }


def test_with_no_word_on_a_game_the_stored_row_is_left_alone() -> None:
    assert live_fields(stored(KNICKS), None, None) == {}


# ---------------------------------------------------------------------------
# GET /v1/games/{date}, through the service
# ---------------------------------------------------------------------------


class Slate:
    """GamesService.get_games_on_date over handed-in games, snapshots and scoreboard."""

    def __init__(self, monkeypatch):
        self.games: list[dict] = []
        self.snapshots: dict[str, LatestSnapshot] = {}
        self.scoreboard: list[dict] | Exception = []
        self.scoreboard_reads = 0
        self.db_reads: list[str] = []

        async def run_db(name, fn, *args):
            self.db_reads.append(fn.__name__)
            if fn.__name__ == "_stored_today":
                return self.games, self.snapshots
            return self.games

        async def run_blocking_provider(provider, operation, fn, *args):
            assert (provider, operation) == ("nba", "games_scoreboard")
            self.scoreboard_reads += 1
            if isinstance(self.scoreboard, Exception):
                raise self.scoreboard
            return self.scoreboard

        monkeypatch.setattr(games_service, "run_db", run_db)
        monkeypatch.setattr(games_service, "run_blocking_provider", run_blocking_provider)
        monkeypatch.setattr(games_service, "_get_nba_today", lambda: TODAY)

    def get(self, day: date = TODAY):
        with freeze_time(NOW):
            resp = asyncio.run(GamesService.get_games_on_date(day))
        assert resp.status == ApiStatus.SUCCESS
        return {game.game_id: game for game in resp.data.games}


@pytest.fixture
def slate(monkeypatch) -> Slate:
    return Slate(monkeypatch)


def test_todays_slate_is_the_pipelines_snapshots_and_asks_the_nba_nothing(slate) -> None:
    slate.games = [stored(KNICKS), stored(LAKERS, "LAL", "PHX"), stored(SUNS, "DEN", "UTA")]
    slate.snapshots = {
        KNICKS: snapshot(),
        LAKERS: snapshot(status=3, period=4, clock=None, home=110, away=106, age=3600),
        SUNS: snapshot(status=1, period=None, clock=None, home=0, away=0),
    }

    games = slate.get()

    assert slate.scoreboard_reads == 0 and slate.db_reads == ["_stored_today"]
    live = games[KNICKS]
    assert (live.status, live.period, live.game_clock, live.home_score, live.away_score) == (
        "in_progress", 2, "PT04M41.00S", 49, 51)
    assert (games[LAKERS].status, games[LAKERS].home_score, games[LAKERS].period) == ("final", 110, 4)
    assert (games[SUNS].status, games[SUNS].period, games[SUNS].game_clock) == ("scheduled", None, None)
    # What the snapshot does not carry is still the stored game's.
    assert (live.arena, live.start_time_et, live.home_team) == ("Madison Square Garden", "19:30", "NYK")


def test_before_the_pipelines_window_the_scoreboard_is_read_as_it_was(slate) -> None:
    slate.games = [stored(KNICKS), stored(LAKERS, "LAL", "PHX")]
    slate.scoreboard = [listed(KNICKS, status=1, period=0, clock="", home=0, away=0)]

    games = slate.get()

    assert slate.scoreboard_reads == 1
    assert (games[KNICKS].status, games[KNICKS].period, games[KNICKS].game_clock, games[KNICKS].home_score) == (
        "scheduled", 0, "", 0)
    # A stored game the scoreboard does not list is left as stored.
    assert (games[LAKERS].status, games[LAKERS].home_score, games[LAKERS].period) == ("scheduled", None, None)


def test_a_stalled_pipeline_falls_back_to_whichever_is_further_along(slate) -> None:
    slate.games = [stored(KNICKS), stored(LAKERS, "LAL", "PHX")]
    slate.snapshots = {
        KNICKS: snapshot(period=2, home=49, away=51, age=900),      # stopped a quarter of an hour ago
        LAKERS: snapshot(period=3, home=80, away=77, age=900),
    }
    slate.scoreboard = [
        listed(KNICKS, period=3, clock="PT10M00.00S", home=70, away=66),   # has moved on since
        listed(LAKERS, period=2, clock="PT01M00.00S", home=60, away=58),   # is still behind the snapshot
    ]

    games = slate.get()

    assert slate.scoreboard_reads == 1
    assert (games[KNICKS].period, games[KNICKS].home_score, games[KNICKS].game_clock) == (3, 70, "PT10M00.00S")
    assert (games[LAKERS].period, games[LAKERS].home_score, games[LAKERS].game_clock) == (3, 80, "PT04M41.00S")


def test_a_scoreboard_that_cannot_be_read_leaves_the_snapshots_there_are(slate) -> None:
    slate.games = [stored(KNICKS), stored(LAKERS, "LAL", "PHX")]
    slate.snapshots = {KNICKS: snapshot()}                           # LAKERS has none, so the scoreboard is asked
    slate.scoreboard = RuntimeError("NBA live API timeout")

    games = slate.get()

    assert slate.scoreboard_reads == 1
    assert (games[KNICKS].status, games[KNICKS].home_score) == ("in_progress", 49)
    assert (games[LAKERS].status, games[LAKERS].home_score) == ("scheduled", None)


def test_another_date_is_the_stored_games_with_no_overlay_and_no_snapshot_read(slate) -> None:
    slate.games = [{**stored(KNICKS), "status": "final", "home_score": 110, "away_score": 106}]
    slate.snapshots = {KNICKS: snapshot()}

    games = slate.get(TODAY - timedelta(days=1))

    assert slate.db_reads == ["_stored_games"] and slate.scoreboard_reads == 0
    assert (games[KNICKS].status, games[KNICKS].home_score, games[KNICKS].period) == ("final", 110, None)
