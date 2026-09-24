"""The current-report window, against real SQL.

A player's latest report only counts as their status while it is inside
`current_report_window`. Both readers apply it: `get_current_status` (the
player status endpoint) and `get_injured_players` (NBA team rosters and live
game pages).
"""

import asyncio
from datetime import date, datetime

import pytest
from freezegun import freeze_time

from db.models.nba.player_injuries import PlayerInjury
from db.models.nba.players import Player
from services.player_service import PlayerService

pytestmark = pytest.mark.integration

TODAY = date(2026, 9, 21)
SENGUN = 1630578


def _player(player_id: int) -> None:
    Player.create(
        id=player_id, name=f"P{player_id}", name_normalized=f"p{player_id}",
        created_at=datetime.utcnow(), updated_at=datetime.utcnow(),
    )


def _report(player_id: int, report_date: date, status: str) -> None:
    PlayerInjury.create(player=player_id, report_date=report_date, status=status)


@pytest.fixture
def reports(integration_db):
    # Last season's final report, never refreshed over the summer.
    _player(SENGUN)
    _report(SENGUN, date(2026, 3, 30), "Questionable")
    _report(SENGUN, date(2026, 4, 12), "Out")
    # Out last season, cleared to Available this week.
    _player(2)
    _report(2, date(2026, 4, 12), "Out")
    _report(2, date(2026, 9, 18), "Available")
    # Currently injured: a report on the oldest day that still counts.
    _player(3)
    _report(3, date(2026, 9, 10), "Questionable")
    _report(3, date(2026, 9, 14), "Out")
    # Injured one day too long ago.
    _player(4)
    _report(4, date(2026, 9, 13), "Doubtful")
    yield


def test_last_seasons_report_is_not_a_current_status(reports):
    assert PlayerInjury.get_current_status(SENGUN, as_of=TODAY) is None
    # History is still there for anything that wants it.
    assert PlayerInjury.get_player_injury_history(SENGUN)[0].report_date == date(2026, 4, 12)


def test_newest_report_in_the_window_wins(reports):
    assert PlayerInjury.get_current_status(2, as_of=TODAY).status == "Available"
    assert PlayerInjury.get_current_status(3, as_of=TODAY).status == "Out"
    assert PlayerInjury.get_current_status(4, as_of=TODAY) is None


def test_a_report_after_the_as_of_date_is_ignored(reports):
    # Reading an earlier date must not see reports written since.
    assert PlayerInjury.get_current_status(2, as_of=date(2026, 4, 15)).status == "Out"


def test_injured_players_only_counts_current_reports(reports):
    injured = {i.player_id: i.status for i in PlayerInjury.get_injured_players(TODAY)}
    assert injured == {3: "Out"}


def test_injured_players_on_last_seasons_final_day(reports):
    injured = {i.player_id: i.status for i in PlayerInjury.get_injured_players(date(2026, 4, 12))}
    assert injured == {SENGUN: "Out", 2: "Out"}


@freeze_time("2026-09-21T16:00:00Z")  # noon ET
def test_status_endpoint_service_end_to_end(reports):
    stale = asyncio.run(PlayerService.get_player_status(SENGUN))
    assert stale.data is None
    assert stale.message == "No current injury report"

    current = asyncio.run(PlayerService.get_player_status(3))
    assert current.data.status == "Out"
    assert current.data.report_date == "2026-09-14"
    assert current.data.report_age_days == 7
