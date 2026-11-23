"""
GET /v1/live/players/today says how old the newest of its lines is
(`live_age_ms`, core/live_age.py), read off the rows the live pipeline stamps.
"""

import inspect
from datetime import date, datetime, timedelta

import pytest
from freezegun import freeze_time

from api.v1.public import live
from db.models.nba.live_player_stats import LivePlayerStats
from db.models.nba.players import Player
from schemas.live import LivePlayersResp

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 16, 1, 30, 16)      # 8:30 PM ET on the 15th's slate; UTC and naive like `last_updated`
TONIGHT = date(2026, 11, 15)
LINE = {"pts": 20, "reb": 5, "ast": 5, "stl": 1, "blk": 1, "tov": 2, "min": 30,
        "fgm": 8, "fga": 16, "fg3m": 2, "fg3a": 5, "ftm": 2, "fta": 2}


def leaders() -> LivePlayersResp:
    """The handler under its rate-limit and executor wrappers, on this connection."""
    with freeze_time(NOW):
        return LivePlayersResp(**inspect.unwrap(live.get_live_players_today)(None))


def written(player_id: int, game_id: str, seconds_ago: float, day: date = TONIGHT, status: int = 2) -> None:
    Player.get_or_create(id=player_id, defaults={"espn_id": 9000 + player_id, "name": f"P{player_id}",
                                                 "name_normalized": f"p{player_id}"})
    LivePlayerStats.create(player=player_id, game_id=game_id, game_date=day, game_status=status, fpts=30,
                           last_updated=NOW - timedelta(seconds=seconds_ago), **LINE)


def test_the_age_is_the_newest_line_of_tonights(integration_db):
    written(1, "0022600236", 1.6)
    written(2, "0022600237", 1.2)                       # the game written last on the run
    written(3, "0022600230", 7200, status=3)            # an early game, settled and no longer read
    written(4, "0022600229", 0.1, day=TONIGHT - timedelta(days=1))   # not tonight's

    data = leaders().data
    assert data.player_count == 3
    assert data.live_age_ms == 1200


def test_no_lines_tonight_no_age(integration_db):
    data = leaders().data
    assert data.player_count == 0 and data.live_age_ms is None
