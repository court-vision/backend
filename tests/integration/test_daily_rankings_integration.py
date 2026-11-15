"""The daily boards over a real database (services/valuation/daily.py) and the table
they are stored in (nba.daily_rankings, migration 0050).

Today is pinned to Nov 16, 2026, inside the 2026-27 calendar.
"""

from datetime import date
from uuid import uuid4

import pytest
from peewee import IntegrityError

from core.nba_calendar import nba_date_et
from core.settings import settings
from db.base import db
from db.models.nba.daily_rankings import (
    BOARD_CATEGORIES,
    BOARD_POINTS,
    SOURCE_CV,
    SOURCE_ESPN,
    DailyRanking,
)
from db.models.nba.player_injuries import PlayerInjury
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.players import Player
from services import schedule_service
from services.scoring.pool import baseline_season
from services.valuation import daily

pytestmark = pytest.mark.integration

TODAY = date(2026, 11, 16)
STAR, HURT, NEW = 920_001, 920_002, 920_003


def _season_row(player_id, season, gp, per_game, as_of):
    return PlayerSeasonStats.create(
        player=player_id, team=None, as_of_date=as_of, season=season, gp=gp,
        fpts=int(per_game * gp), pts=int(per_game * gp), reb=gp * 5, ast=gp * 3, stl=gp, blk=gp,
        tov=gp * 2, min=gp * 30, fgm=gp * 8, fga=gp * 16, fg3m=gp * 2, fg3a=gp * 5, ftm=gp * 4, fta=gp * 5,
    )


@pytest.fixture
def season(integration_db, monkeypatch):
    monkeypatch.setattr(settings, "nba_season", "2026-27", raising=False)
    monkeypatch.setattr(schedule_service, "get_nba_today", lambda: TODAY)
    last = baseline_season()
    for pid, name in ((STAR, "Steady Star"), (HURT, "Hurt Today"), (NEW, "New Face")):
        Player.create(id=pid, espn_id=pid - 900_000, name=name, name_normalized=name.lower(), position="G")
    _season_row(STAR, last, 70, 30.0, date(2026, 4, 12))
    _season_row(HURT, last, 70, 24.0, date(2026, 4, 12))
    _season_row(STAR, "2026-27", 10, 40.0, date(2026, 11, 15))
    _season_row(NEW, "2026-27", 12, 20.0, date(2026, 11, 15))
    PlayerInjury.upsert_injury(HURT, nba_date_et(), "Out")


def test_the_morning_board_reads_this_season_and_todays_report(season):
    boards = daily.build(daily.load_inputs(), TODAY)

    assert (boards.in_season, boards.as_of, boards.model) == (True, TODAY, daily.MODEL_IN_SEASON)
    assert boards.season == "2026-27" and boards.season_as_of == date(2026, 11, 15)
    by_id = {p.player_id: p for p in boards.players}
    assert set(by_id) == {STAR, HURT, NEW}
    # Ten games at 40 points against a prior of 30 is a 35-point line, which ESPN's
    # default weights score at 51 with the rest of his box score; he leads both boards
    assert by_id[STAR].points.rank == 1 and by_id[STAR].points.value == 51.0
    assert by_id[STAR].categories.rank == 1
    assert by_id[STAR].season_gp == 10 and by_id[HURT].season_gp == 0 and by_id[NEW].season_gp == 12
    # A player with no last season is valued off this one; a hurt one keeps his line and is flagged
    assert by_id[NEW].line_source == "season" and by_id[HURT].line_source == "baseline"
    assert by_id[HURT].injury_status == "OUT" and by_id[STAR].injury_status is None
    assert by_id[HURT].points.value == 40.0
    # Fewer games than a season are left
    assert 0 < by_id[STAR].points.games < 65


def test_a_days_boards_are_stored_replaced_and_read_back(season):
    boards = daily.build(daily.load_inputs(), TODAY)
    run_id = uuid4()

    def rows(board):
        return [
            {"player_id": p.player_id, "rank": getattr(p, board).rank, "value": getattr(p, board).value,
             "score": getattr(p, board).score, "games": getattr(p, board).games, "season_gp": p.season_gp,
             "injury_status": p.injury_status, "line_source": p.line_source, "model": boards.model}
            for p in boards.players
        ]

    with db.atomic():
        for board in (BOARD_POINTS, BOARD_CATEGORIES):
            assert DailyRanking.replace_day(TODAY, boards.season, SOURCE_CV, board, rows(board), run_id) == 3
        # ESPN ranks two of them, and has no value to give
        DailyRanking.replace_day(TODAY, boards.season, SOURCE_ESPN, BOARD_POINTS,
                                 [{"player_id": HURT, "rank": 1, "value": 55}, {"player_id": STAR, "rank": 2}])

    assert DailyRanking.stored(TODAY, SOURCE_CV) == 6 and DailyRanking.stored(TODAY, SOURCE_ESPN) == 2
    assert DailyRanking.stored(date(2026, 11, 15), SOURCE_CV) == 0
    points = DailyRanking.ranks_on(TODAY, SOURCE_CV, BOARD_POINTS)
    assert points == {p.player_id: p.points.rank for p in boards.players}
    assert DailyRanking.ranks_on(TODAY, SOURCE_ESPN, BOARD_POINTS) == {HURT: 1, STAR: 2}
    assert DailyRanking.ranks_on(TODAY, SOURCE_ESPN, BOARD_CATEGORIES) == {}

    star = DailyRanking.get((DailyRanking.as_of_date == TODAY) & (DailyRanking.source == SOURCE_CV)
                            & (DailyRanking.board == BOARD_POINTS) & (DailyRanking.player == STAR))
    assert (star.rank, float(star.value), star.season_gp, star.line_source) == (1, 51.0, 10, "baseline")
    assert star.model == daily.MODEL_IN_SEASON and star.pipeline_run_id == run_id and star.season == "2026-27"
    hurt = DailyRanking.get((DailyRanking.as_of_date == TODAY) & (DailyRanking.source == SOURCE_ESPN)
                            & (DailyRanking.player == HURT))
    assert float(hurt.value) == 55.0 and hurt.score is None and hurt.games is None and hurt.model is None

    # A rerun replaces the day's board whole: a player who left it is gone, not left behind
    with db.atomic():
        DailyRanking.replace_day(TODAY, boards.season, SOURCE_CV, BOARD_POINTS,
                                 [{"player_id": NEW, "rank": 1, "value": 20.0}])
    assert DailyRanking.ranks_on(TODAY, SOURCE_CV, BOARD_POINTS) == {NEW: 1}
    assert DailyRanking.stored(TODAY, SOURCE_CV) == 4

    # Yesterday's board is its own; the newest day is found per source, and before a day
    with db.atomic():
        DailyRanking.replace_day(date(2026, 11, 15), boards.season, SOURCE_CV, BOARD_POINTS,
                                 [{"player_id": STAR, "rank": 1}])
    assert DailyRanking.latest_date(SOURCE_CV) == TODAY
    assert DailyRanking.latest_date(SOURCE_CV, season="2026-27", before=TODAY) == date(2026, 11, 15)
    assert DailyRanking.latest_date(SOURCE_ESPN, before=TODAY) is None
    assert DailyRanking.latest_date("analyst") is None


def test_one_row_per_player_per_board_per_source_per_day(season):
    DailyRanking.create(as_of_date=TODAY, season="2026-27", source=SOURCE_CV, board=BOARD_POINTS,
                        player=STAR, rank=1)
    with pytest.raises(IntegrityError), db.atomic():
        DailyRanking.create(as_of_date=TODAY, season="2026-27", source=SOURCE_CV, board=BOARD_POINTS,
                            player=STAR, rank=2)
    with pytest.raises(IntegrityError), db.atomic():
        DailyRanking.create(as_of_date=TODAY, season="2026-27", source=SOURCE_CV, board="roto",
                            player=STAR, rank=1)
    with pytest.raises(IntegrityError), db.atomic():
        DailyRanking.create(as_of_date=TODAY, season="2026-27", source=SOURCE_CV, board=BOARD_CATEGORIES,
                            player=STAR, rank=0)
    # The same player on the other board, and under ESPN, are different rows
    DailyRanking.create(as_of_date=TODAY, season="2026-27", source=SOURCE_CV, board=BOARD_CATEGORIES,
                        player=STAR, rank=4)
    DailyRanking.create(as_of_date=TODAY, season="2026-27", source=SOURCE_ESPN, board=BOARD_POINTS,
                        player=STAR, rank=9)
    assert DailyRanking.stored(TODAY, SOURCE_CV) == 2
