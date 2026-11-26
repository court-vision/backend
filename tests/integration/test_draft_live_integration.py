"""The stored daily boards beside a Draft lab board in season (services/draft_live.py)
over a real database: nba.daily_rankings as the data platform writes it, and the
room's own read of it (DraftBoardService._season_reads).

Tiny boards written by hand, a day at a time, so every move and date in the
answers can be read off the test.
"""

from datetime import date, datetime, timedelta

import pytest

from core.settings import settings
from db.base import db
from db.models.drafts import DraftSession
from db.models.nba.daily_rankings import (
    BOARD_CATEGORIES,
    BOARD_POINTS,
    SOURCE_CV,
    SOURCE_ESPN,
    DailyRanking,
)
from db.models.nba.players import Player
from db.models.users import User
from services import draft_live, schedule_service
from services.draft_board_service import DraftBoardService
from services.draft_live import EspnNow

pytestmark = pytest.mark.integration

SEASON = "2026-27"
TODAY = date(2026, 11, 23)
YESTERDAY = TODAY - timedelta(days=1)
WEEK_AGO = TODAY - timedelta(days=7)
FIRST = date(2026, 11, 17)
STAR, GUARD, WING, BIG = 940_001, 940_002, 940_003, 940_004
SHADOW = "cv_shadow"    # a candidate board graded in shadow: stored beside the plain one, never read


def store(day, source, which, ranks, season=SEASON):
    """One board of one source for one day: `ranks` is {player_id: rank} or {player_id: (rank, value)}."""
    rows = []
    for player_id, rank in ranks.items():
        rank, value = rank if isinstance(rank, tuple) else (rank, None)
        rows.append({"player_id": player_id, "rank": rank, "value": value})
    with db.atomic():
        DailyRanking.replace_day(day, season, source, which, rows)


@pytest.fixture
def players(integration_db, monkeypatch):
    monkeypatch.setattr(settings, "nba_season", SEASON, raising=False)
    monkeypatch.setattr(schedule_service, "get_nba_today", lambda: TODAY)
    for pid, name in ((STAR, "Steady Star"), (GUARD, "Rising Guard"), (WING, "Falling Wing"), (BIG, "New Big")):
        Player.create(id=pid, espn_id=pid - 900_000, name=name, name_normalized=name.lower(), position="G")


def a_week_of_boards():
    """Points boards a week ago, yesterday and today, 9-cat today; ESPN's points ranks every morning since the 17th."""
    store(WEEK_AGO, SOURCE_CV, BOARD_POINTS, {STAR: 1, WING: 2, GUARD: 9})
    store(YESTERDAY, SOURCE_CV, BOARD_POINTS, {STAR: 1, GUARD: 4, WING: 5})
    store(TODAY, SOURCE_CV, BOARD_POINTS, {STAR: 1, GUARD: 3, WING: 7, BIG: 12})
    store(TODAY, SOURCE_CV, BOARD_CATEGORIES, {STAR: 2, GUARD: 1})
    day = FIRST
    while day <= TODAY:
        ranks = {STAR: (1, 60.0), GUARD: (9 if day < date(2026, 11, 21) else 6, 20.0)}
        if day <= date(2026, 11, 19):
            ranks[WING] = (2, 55.0)     # ESPN stops ranking him on the 20th
        store(day, SOURCE_ESPN, BOARD_POINTS, ranks)
        day += timedelta(days=1)
    # What must never be read: the shadow board, with everyone reversed
    store(TODAY, SHADOW, BOARD_POINTS, {BIG: 1, WING: 2, GUARD: 3, STAR: 4})
    store(YESTERDAY, SHADOW, BOARD_POINTS, {BIG: 1, WING: 2, GUARD: 3, STAR: 4})


def test_moves_are_measured_between_the_stored_boards(players):
    a_week_of_boards()

    found = draft_live.load(SEASON, TODAY)

    assert (found.as_of, found.day, found.week) == (TODAY, YESTERDAY, WEEK_AGO)
    points = found.board(categories=False)
    assert points.moves == {STAR: (0, 0), GUARD: (1, 6), WING: (-2, -5), BIG: (None, None)}
    # The 9-cat board was first stored today: he is ranked, with nothing to measure a move against
    assert found.board(categories=True).moves == {STAR: (None, None), GUARD: (None, None)}


def test_espns_rank_is_the_mornings_and_carries_the_day_it_last_changed(players):
    a_week_of_boards()

    points = draft_live.load(SEASON, TODAY).board(categories=False)

    assert points.espn_as_of == TODAY
    # Held since the first morning its ranks were stored: at least that old
    assert points.espn[STAR] == EspnNow(rank=1, value=60.0, since=FIRST, since_first_stored=True)
    assert points.espn[GUARD] == EspnNow(rank=6, value=20.0, since=date(2026, 11, 21), since_first_stored=False)
    # ESPN stopped ranking the wing; it never ranked the big
    assert WING not in points.espn and BIG not in points.espn
    # Its 9-cat ranks were never stored: nothing to read for a category room
    cats = draft_live.load(SEASON, TODAY).board(categories=True)
    assert cats.espn_as_of is None and cats.espn == {}


def test_boards_that_are_days_old_are_not_read(players):
    a_week_of_boards()

    # Four days on with nothing stored since: a move or an age read off the 23rd would describe last week
    assert draft_live.load(SEASON, TODAY + timedelta(days=4)) is None
    # Three days on they still stand
    late = draft_live.load(SEASON, TODAY + timedelta(days=3))
    assert late.as_of == TODAY and late.board(categories=False).espn_as_of == TODAY
    # A board counting from an earlier day reads nothing stored after it
    early = draft_live.load(SEASON, YESTERDAY)
    assert early.as_of == YESTERDAY and early.board(categories=False).espn_as_of == YESTERDAY
    assert early.board(categories=False).espn[GUARD].since == date(2026, 11, 21)
    assert draft_live.load("2025-26", TODAY) is None


def test_espns_ranks_stand_alone_on_a_morning_no_board_was_stored(players):
    store(TODAY, SOURCE_ESPN, BOARD_POINTS, {STAR: 1})

    found = draft_live.load(SEASON, TODAY)

    assert found.as_of is None and found.board(categories=False).moves == {}
    assert found.board(categories=False).espn[STAR].since == TODAY


def test_the_read_is_shared_for_a_few_minutes(players):
    a_week_of_boards()
    first = draft_live.load(SEASON, TODAY)
    store(TODAY, SOURCE_CV, BOARD_POINTS, {GUARD: 1, STAR: 2})

    assert draft_live.load(SEASON, TODAY) is first
    draft_live.clear_cache()
    assert draft_live.load(SEASON, TODAY).board(categories=False).moves[GUARD] == (3, 8)


def test_a_room_in_season_reads_them_and_the_shadow_boards_do_not(players):
    a_week_of_boards()
    user = User.create(email="draftlive@courtvision.dev", clerk_user_id="user_draftlive", created_at=datetime.utcnow())
    room = DraftSession.create(user=user.user_id, kind="mock", status="active", draft_type="snake",
                               pick_order=[1, 2], my_slot=1, rounds=13, created_at=datetime(2026, 11, 22, 12))

    inputs = DraftBoardService._fetch_inputs(frozenset(), room.id)

    assert inputs.in_season is not None and inputs.stored_ranks.as_of == TODAY
    assert inputs.stored_ranks.board(categories=False).moves[GUARD] == (1, 6)
    # The boards built on the analyst's projection are graded, never shown: they read none of this
    shadow = DraftBoardService._fetch_inputs(frozenset(), None, "cv_live_shadow")
    assert shadow.in_season is not None and shadow.stored_ranks is None
    # Nor does a room that drafted before opening night
    early = DraftSession.create(user=user.user_id, kind="mock", status="completed", draft_type="snake",
                                pick_order=[1, 2], my_slot=1, rounds=13, created_at=datetime(2026, 10, 1, 12),
                                started_at=datetime(2026, 10, 12, 1))
    assert DraftBoardService._fetch_inputs(frozenset(), early.id).stored_ranks is None
