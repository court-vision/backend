"""The draft board in season over a real database: this season's stat rows,
today's injury report and the room's own dates (services/draft_in_season.py).

Today is pinned to Nov 15, 2026, inside the 2026-27 calendar, so the test reads
the same whatever day it runs.
"""

from datetime import date, datetime

import pytest

from core.nba_calendar import nba_date_et
from core.settings import settings
from db.models.drafts import DraftSession
from db.models.nba.draft_market import DraftMarket
from db.models.nba.player_injuries import PlayerInjury
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.players import Player
from db.models.users import User
from services import schedule_service
from services.draft_board_service import DraftBoardService
from services.scoring.pool import baseline_season

pytestmark = pytest.mark.integration

TODAY = date(2026, 11, 15)


def _season_row(player_id, season, gp, per_game, as_of):
    return PlayerSeasonStats.create(
        player=player_id, team=None, as_of_date=as_of, season=season, gp=gp,
        fpts=int(per_game * gp), pts=int(per_game * gp), reb=gp * 5, ast=gp * 3, stl=gp, blk=gp,
        tov=gp * 2, min=gp * 30, fgm=gp * 8, fga=gp * 16, fg3m=gp * 2, fg3a=gp * 5, ftm=gp * 4, fta=gp * 5,
    )


@pytest.fixture
def players(integration_db, monkeypatch):
    monkeypatch.setattr(settings, "nba_season", "2026-27", raising=False)
    monkeypatch.setattr(schedule_service, "get_nba_today", lambda: TODAY)
    last = baseline_season()
    for pid, name in ((910_001, "Hot Start"), (910_002, "Newly Hurt"), (910_003, "Late Arrival")):
        Player.create(id=pid, espn_id=pid - 900_000, name=name, name_normalized=name.lower(), position="G")
    _season_row(910_001, last, 70, 30.0, date(2026, 4, 12))
    _season_row(910_002, last, 70, 28.0, date(2026, 4, 12))
    # This season: ten games at 40 a game for the first, nothing yet for the second,
    # and twelve for a player with no last season to blend with
    _season_row(910_001, "2026-27", 10, 40.0, date(2026, 11, 14))
    _season_row(910_003, "2026-27", 12, 25.0, date(2026, 11, 14))
    for pid in (910_001, 910_002):
        DraftMarket.create(player=pid, season="2026-27", source="espn", as_of_date=date(2026, 10, 31),
                           overall_rank=pid - 910_000, adp=float(pid - 910_000), injury_status=None)
    PlayerInjury.upsert_injury(910_002, nba_date_et(), "Out")
    return User.create(email="inseason@courtvision.dev", clerk_user_id="user_inseason",
                       created_at=datetime.utcnow())


def _room(user, **fields):
    base = dict(user=user.user_id, kind="live", status="active", draft_type="snake", pick_order=[1, 2],
                my_slot=1, rounds=13, created_at=datetime(2026, 10, 1, 12))
    base.update(fields)
    return DraftSession.create(**base)


def test_a_room_drafting_now_values_on_this_season(players):
    # Made in October, no pick yet: it drafts now
    room = _room(players)

    inputs = DraftBoardService._fetch_inputs(frozenset(), room.id)

    assert inputs.in_season is not None and inputs.in_season.games_from == TODAY
    by_id = {row.id: row for row in inputs.pool}
    # Half last season's 30, half this season's 40
    assert by_id[910_001].fpts_avg == 35.0
    assert by_id[910_002].fpts_avg == 28.0
    assert inputs.source[910_003] == "season" and by_id[910_003].fpts_avg == 25.0
    assert inputs.in_season.season_gp == {910_001: 10, 910_003: 12}
    # Today's report, an injury the Oct 31 snapshot didn't have
    assert inputs.live_injury == {910_002: "OUT"} and inputs.new_injury == frozenset({910_002})
    # Fewer games than a full season are left
    assert 0 < inputs.games_left[910_001] < 65
    assert sum(len(w.days) for w in inputs.calendar) < sum(
        len(w.days) for w in DraftBoardService._calendar_weeks())


def test_a_room_that_drafted_before_opening_night_keeps_its_preseason_values(players):
    early = _room(players, started_at=datetime(2026, 10, 12, 1))
    imported = _room(players, kind="import", status="completed", started_at=datetime(2026, 11, 14, 2))

    for room in (early, imported):
        inputs = DraftBoardService._fetch_inputs(frozenset(), room.id)
        assert inputs.in_season is None and inputs.live_injury is None
        assert {row.id: row.fpts_avg for row in inputs.pool}[910_001] == 30.0
        assert 910_003 not in {row.id for row in inputs.pool}
