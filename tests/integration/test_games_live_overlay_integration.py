"""Today's slate read off the live pipeline's snapshots (GET /v1/games/{today}), against the real migrations.

The rules for which word on a game is taken are tests/unit/test_games_live_overlay.py;
this is the query behind them and the service on a real connection.
"""

import asyncio
from datetime import date, datetime, timedelta

import pytest
from freezegun import freeze_time

from db.base import start_db_runtime, stop_db_runtime
from db.models.nba.games import Game
from db.models.nba.live_game_score_snapshots import LatestSnapshot, LiveGameScoreSnapshot
from db.models.nba.teams import NBATeam
from schemas.common import ApiStatus
from services import game_detail_service, games_service
from services.game_detail_service import score_history
from services.games_service import GamesService

pytestmark = pytest.mark.integration

TODAY = date(2026, 11, 10)
NOW = datetime(2026, 11, 11, 1, 30, 0)
KNICKS, LAKERS, SUNS = "0022600151", "0022600152", "0022600153"


@pytest.fixture(autouse=True)
def night(clean_tables, monkeypatch):
    monkeypatch.setattr(games_service, "_get_nba_today", lambda: TODAY)
    for team in ("NYK", "BOS", "LAL", "PHX", "DEN", "UTA"):
        NBATeam.get_or_create(id=team, defaults={"name": team, "city": team, "conference": "East", "division": "Atlantic"})
    for game_id, home, away in ((KNICKS, "NYK", "BOS"), (LAKERS, "LAL", "PHX"), (SUNS, "DEN", "UTA")):
        Game.create(game_id=game_id, game_date=TODAY, season="2026-27", home_team=home, away_team=away, status="scheduled")


def snap(game_id, seconds_ago, home, away, status=2, period=1, clock="PT07M20.00S"):
    LiveGameScoreSnapshot.create(
        game_id=game_id, game_date=TODAY, home_team="NYK", away_team="BOS", home_score=home, away_score=away,
        period=period, game_clock=clock, game_status=status, captured_at=NOW - timedelta(seconds=seconds_ago),
    )


def test_latest_by_game_is_each_games_newest_snapshot():
    snap(KNICKS, 90, 28, 25)
    snap(KNICKS, 30, 49, 51, period=2, clock="PT04M41.00S")
    snap(KNICKS, 60, 40, 41, period=2, clock="PT09M00.00S")
    snap(LAKERS, 45, 0, 0, status=1, period=None, clock=None)

    latest = LiveGameScoreSnapshot.latest_by_game([KNICKS, LAKERS, SUNS, None])

    assert latest == {
        KNICKS: LatestSnapshot(2, 2, "PT04M41.00S", 49, 51, NOW - timedelta(seconds=30)),
        LAKERS: LatestSnapshot(1, None, None, 0, 0, NOW - timedelta(seconds=45)),
    }
    assert LiveGameScoreSnapshot.latest_by_game([]) == {}


def test_todays_slate_reads_the_snapshots_and_not_the_scoreboard(monkeypatch):
    snap(KNICKS, 20, 49, 51, period=2, clock="PT04M41.00S")
    snap(LAKERS, 5400, 110, 106, status=3, period=4, clock=None)
    snap(SUNS, 20, 0, 0, status=1, period=None, clock=None)

    async def no_scoreboard(*_args, **_kwargs):
        raise AssertionError("the scoreboard was read with a fresh snapshot of every game")

    monkeypatch.setattr(games_service, "run_blocking_provider", no_scoreboard)

    async def get():
        start_db_runtime()
        try:
            return await GamesService.get_games_on_date(TODAY)
        finally:
            await stop_db_runtime()

    with freeze_time(NOW):
        resp = asyncio.run(get())

    assert resp.status == ApiStatus.SUCCESS
    games = {game.game_id: game for game in resp.data.games}
    assert (games[KNICKS].status, games[KNICKS].period, games[KNICKS].game_clock, games[KNICKS].home_score) == (
        "in_progress", 2, "PT04M41.00S", 49)
    assert (games[LAKERS].status, games[LAKERS].home_score, games[LAKERS].away_score) == ("final", 110, 106)
    assert (games[SUNS].status, games[SUNS].period) == ("scheduled", None)


def test_score_history_keeps_the_ends_of_a_run_of_one_score_though_the_clock_moves():
    """The snapshots are the live box score's: the clock differs in every one through play."""
    for i, (home, away) in enumerate([(2, 0), (2, 0), (2, 0), (2, 0), (4, 0), (4, 3), (4, 3), (4, 3)]):
        snap(KNICKS, 600 - 30 * i, home, away, clock=f"PT{11 - i:02d}M00.00S")

    history = score_history(KNICKS)

    assert [(h.home_score, h.away_score, h.game_clock) for h in history] == [
        (2, 0, "PT11M00.00S"), (2, 0, "PT08M00.00S"), (4, 0, "PT07M00.00S"),
        (4, 3, "PT06M00.00S"), (4, 3, "PT04M00.00S"),
    ]
