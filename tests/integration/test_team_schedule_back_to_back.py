"""Team schedule: back-to-backs from one date query, byte-identical to the per-game EXISTS.

The schedule used to ask `Game.is_back_to_back` once per listed game (82
queries for a full season, 90-110 ms of every team sheet in production). It now
reads the team's game dates once. The reference below is the service as it was
before that change, kept verbatim, so every case here proves the new body is the
old body byte for byte, including the edges the old query got right by asking
about any game at all: a preseason game the night before opening night, a game
of another season type or season label, an unlabelled game.
"""

from datetime import date
from decimal import Decimal

import pytest
from freezegun import freeze_time
from peewee import OperationalError

from core.nba_calendar import nba_date_et
from core.settings import get_settings, settings as app_settings
from db.base import db
from db.models.nba.games import FANTASY_SEASON_TYPES, Game
from db.models.nba.team_stats import TeamStats
from db.models.nba.teams import NBATeam
from schemas.common import ApiStatus
from schemas.teams import ScheduleGame, TeamScheduleData, TeamScheduleResp
from services.team_schedule_service import TeamScheduleService

pytestmark = pytest.mark.integration

SEASON = "2025-26"  # matchup 1 of the 2025-26 calendar starts 2025-10-21
TEAMS = ("DEN", "LAL", "BOS", "PHX", "OKC")


def _schedule_before_the_fix(team_abbrev: str, upcoming: bool = False, limit: int = 20) -> TeamScheduleResp:
    """TeamScheduleService.get_team_schedule as of origin/main ac5f69a (error paths trimmed)."""
    team_id = team_abbrev.upper()
    team = NBATeam.get_or_none(NBATeam.id == team_id)
    if not team:
        return TeamScheduleResp(status=ApiStatus.NOT_FOUND, message=f"Team '{team_id}' not found", data=None)

    from services.schedule_service import get_matchup_by_number
    settings = get_settings()
    if upcoming:
        start_date = nba_date_et()
    else:
        matchup_1 = get_matchup_by_number(1)
        start_date = matchup_1["start_date"] if matchup_1 else None
    games = Game.get_team_games(
        team_id=team_id,
        start_date=start_date,
        season=settings.nba_season,
        season_types=FANTASY_SEASON_TYPES,
    )
    games = games[:limit]

    def_rating_map: dict[str, float] = {}
    for ts in TeamStats.get_all_latest():
        if ts.def_rating is not None:
            def_rating_map[ts.team_id] = float(ts.def_rating)

    schedule = []
    for g in games:
        is_home = g.home_team_id == team_id
        opponent = g.away_team_id if is_home else g.home_team_id
        team_score = g.home_score if is_home else g.away_score
        opponent_score = g.away_score if is_home else g.home_score
        schedule.append(
            ScheduleGame(
                date=g.game_date.isoformat(),
                opponent=opponent,
                home=is_home,
                back_to_back=Game.is_back_to_back(team_id, g.game_date),
                status=g.status,
                team_score=team_score,
                opponent_score=opponent_score,
                opponent_def_rating=def_rating_map.get(opponent),
            )
        )
    remaining = sum(1 for g in schedule if g.status == "scheduled")
    return TeamScheduleResp(
        status=ApiStatus.SUCCESS,
        message=f"Schedule for {team.name}",
        data=TeamScheduleData(
            team=team_id,
            team_name=team.name,
            schedule=schedule,
            remaining_games=remaining,
            total_games=len(schedule),
        ),
    )


def game(game_id, day, home, away, *, status="scheduled", score=None, season=SEASON):
    home_score, away_score = score or (None, None)
    return Game.create(
        game_id=game_id, game_date=day, season=season, home_team=home, away_team=away,
        status=status, home_score=home_score, away_score=away_score,
    )


@pytest.fixture(autouse=True)
def season_slate(clean_tables, monkeypatch):
    # The service reads a fresh Settings() (the environment); the calendar reads the singleton.
    monkeypatch.setenv("NBA_SEASON", SEASON)
    monkeypatch.setattr(app_settings, "nba_season", SEASON)
    db.execute_sql("TRUNCATE nba.team_stats RESTART IDENTITY")
    names = {"DEN": "Denver Nuggets", "LAL": "Los Angeles Lakers", "BOS": "Boston Celtics",
             "PHX": "Phoenix Suns", "OKC": "Oklahoma City Thunder"}
    for team in TEAMS:
        NBATeam.get_or_create(id=team, defaults={"name": names[team], "city": team,
                                                "conference": "West", "division": "Northwest"})

    # Denver: a preseason game the night before opening night (not listed, but a
    # back-to-back for the opener), a three-night run, a play-in-typed game and
    # a game filed under another season label (neither listed, both count), an
    # unlabelled game (listed), and a night off after another team's game.
    game("0012500001", date(2025, 10, 20), "DEN", "PHX", status="final", score=(101, 99))
    game("0022500001", date(2025, 10, 21), "DEN", "LAL", status="final", score=(118, 110))
    game("0022500002", date(2025, 10, 23), "BOS", "DEN", status="final", score=(104, 112))
    game("0022500003", date(2025, 10, 24), "DEN", "PHX", status="final", score=(120, 120))
    game("0022500004", date(2025, 10, 26), "LAL", "DEN")
    game("0022500005", date(2025, 10, 27), "DEN", "OKC")
    game("0022500006", date(2025, 10, 28), "PHX", "DEN")
    game("0052500001", date(2025, 10, 29), "DEN", "BOS")
    game("0022500007", date(2025, 10, 30), "DEN", "BOS")
    game("0022400999", date(2025, 11, 1), "OKC", "DEN", season="2024-25")
    game("X-UNLABELLED", date(2025, 11, 2), "DEN", "LAL")
    game("0022500008", date(2025, 11, 4), "LAL", "BOS")
    game("0022500009", date(2025, 11, 5), "DEN", "LAL")
    # A game on the far side of the season window, which must not leak in.
    game("0042500001", date(2026, 4, 20), "DEN", "OKC")

    # Two stats dates: only the latest counts; OKC has no rating that day.
    for team, old, new in (("LAL", "110.0", "112.4"), ("BOS", "108.0", "109.9"),
                           ("PHX", "115.0", "114.1"), ("OKC", "104.0", None), ("DEN", "111.0", "111.5")):
        TeamStats.create(team=team, as_of_date=date(2025, 11, 1), season=SEASON, def_rating=Decimal(old))
        TeamStats.create(team=team, as_of_date=date(2025, 11, 3), season=SEASON,
                         def_rating=Decimal(new) if new else None)
    yield
    db.execute_sql("TRUNCATE nba.team_stats RESTART IDENTITY")


class QueryCounter:
    def __init__(self):
        self.statements: list[str] = []

    def __call__(self, real):
        def counting(sql, params=None, *args, **kwargs):
            self.statements.append(sql)
            return real(sql, params, *args, **kwargs)
        return counting


@pytest.fixture
def count_queries(monkeypatch):
    counter = QueryCounter()
    monkeypatch.setattr(db, "execute_sql", counter(db.execute_sql))
    return counter


@pytest.mark.parametrize("team", ["DEN", "den", "LAL", "BOS", "PHX", "OKC", "XXX"])
@pytest.mark.parametrize("limit", [1, 2, 5, 20, 200])
def test_full_schedule_is_byte_identical_to_the_per_game_query(team, limit):
    new = TeamScheduleService.get_team_schedule.__wrapped__(team, upcoming=False, limit=limit)
    old = _schedule_before_the_fix(team, upcoming=False, limit=limit)

    assert new.model_dump_json() == old.model_dump_json()


@pytest.mark.parametrize("now", [
    "2025-10-24T16:00:00Z",   # noon ET: the list starts on a back-to-back night
    "2025-10-25T09:00:00Z",   # 5 AM ET: still the 24th's NBA date
    "2025-11-03T16:00:00Z",   # after the other season's game
    "2026-05-01T16:00:00Z",   # nothing left to play
])
@pytest.mark.parametrize("limit", [1, 3, 20])
def test_upcoming_schedule_is_byte_identical_to_the_per_game_query(now, limit):
    with freeze_time(now):
        new = TeamScheduleService.get_team_schedule.__wrapped__("DEN", upcoming=True, limit=limit)
        old = _schedule_before_the_fix("DEN", upcoming=True, limit=limit)

    assert new.model_dump_json() == old.model_dump_json()


def test_back_to_backs_and_ratings_are_the_expected_ones():
    """The fixture really exercises the edges (the equivalence would be vacuous otherwise)."""
    resp = TeamScheduleService.get_team_schedule.__wrapped__("DEN", upcoming=False, limit=200)
    rows = {g.date: g for g in resp.data.schedule}

    assert list(rows) == ["2025-10-21", "2025-10-23", "2025-10-24", "2025-10-26", "2025-10-27",
                          "2025-10-28", "2025-10-30", "2025-11-02", "2025-11-05"]
    assert {d for d, g in rows.items() if g.back_to_back} == {
        "2025-10-21",   # the preseason game on the 20th
        "2025-10-24", "2025-10-27", "2025-10-28",
        "2025-10-30",   # the play-in-typed game on the 29th
        "2025-11-02",   # the 2024-25-labelled game on the 1st
    }
    assert rows["2025-11-05"].back_to_back is False      # LAL-BOS on the 4th is not Denver's
    assert rows["2025-10-21"].opponent_def_rating == 112.4
    assert rows["2025-10-27"].opponent_def_rating is None  # OKC: no rating on the latest date
    assert (rows["2025-10-23"].team_score, rows["2025-10-23"].opponent_score) == (112, 104)


def test_query_count_no_longer_grows_with_the_schedule(count_queries):
    TeamScheduleService.get_team_schedule.__wrapped__("DEN", upcoming=False, limit=1)
    one_game = len(count_queries.statements)
    count_queries.statements.clear()

    TeamScheduleService.get_team_schedule.__wrapped__("DEN", upcoming=False, limit=200)
    full = count_queries.statements

    # Team, games, game dates, def ratings: four statements however long the list.
    assert one_game == len(full) == 4
    assert not any(sql.startswith("SELECT 1 FROM") for sql in full)


def test_the_old_shape_asked_once_per_game(count_queries):
    """What the change removed, measured the same way: Peewee's .exists() is a
    `SELECT 1 ... LIMIT 1`, one per listed game, on top of four other statements."""
    _schedule_before_the_fix("DEN", upcoming=False, limit=200)

    probes = [sql for sql in count_queries.statements if sql.startswith('SELECT 1 FROM "nba"."games"')]
    assert len(probes) == 9
    assert len(count_queries.statements) == 13


def test_a_team_without_games_skips_the_date_query(count_queries):
    with freeze_time("2026-05-01T16:00:00Z"):
        resp = TeamScheduleService.get_team_schedule.__wrapped__("LAL", upcoming=True, limit=5)

    assert resp.status == ApiStatus.SUCCESS and resp.data.schedule == []
    assert len(count_queries.statements) == 3  # team, games, def ratings


def test_latest_def_ratings_match_the_latest_rows():
    expected = {ts.team_id: float(ts.def_rating) for ts in TeamStats.get_all_latest() if ts.def_rating is not None}

    assert TeamStats.latest_def_ratings() == expected == {
        "LAL": 112.4, "BOS": 109.9, "PHX": 114.1, "DEN": 111.5,
    }


def test_latest_def_ratings_without_any_stats_is_empty():
    db.execute_sql("TRUNCATE nba.team_stats RESTART IDENTITY")

    assert TeamStats.latest_def_ratings() == {}


def test_a_database_error_in_the_rating_lookup_fails_the_request(monkeypatch):
    """The route caches a success for five minutes, so a dropped connection on
    the last query must not come back as a schedule with every rating blanked."""
    def dropped():
        raise OperationalError("server closed the connection unexpectedly")
    monkeypatch.setattr(TeamStats, "latest_def_ratings", staticmethod(dropped))

    with pytest.raises(OperationalError):
        TeamScheduleService.get_team_schedule.__wrapped__("DEN", upcoming=False, limit=20)


def test_any_other_rating_failure_still_renders_the_schedule(monkeypatch):
    def broken():
        raise ValueError("not a number")
    monkeypatch.setattr(TeamStats, "latest_def_ratings", staticmethod(broken))

    resp = TeamScheduleService.get_team_schedule.__wrapped__("DEN", upcoming=False, limit=20)

    assert resp.status == ApiStatus.SUCCESS
    assert len(resp.data.schedule) == 9
    assert all(g.opponent_def_rating is None for g in resp.data.schedule)
