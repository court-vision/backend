"""
The daily boards: what is stored every morning has to be what the Draft lab
shows a room with no league that morning, so the ranks are checked against the
board itself, in season and before it.
"""

from datetime import date

import pytest

from services import draft_in_season
from services.draft_board_service import BoardInputs, BoardSession, DraftBoardService
from services.draft_congestion import SampleWeek
from services.scoring.category_rank import PoolRow
from services.scoring.models import StatLine
from services.scoring.points import DEFAULT_POINTS
from services.scoring.resolver import resolve_scoring
from services.valuation import daily
from services.valuation.standard import STANDARD_LEAGUE_SIZE, STANDARD_ROUNDS

pytestmark = pytest.mark.unit

TEAMS = ("DEN", "BOS", "LAL", "NYK")
TODAY = date(2026, 11, 16)


def _line(i: int) -> StatLine:
    kind = i % 4
    return StatLine.from_dict({
        "pts": 26.0 - i * 0.35 + (4 if kind == 0 else 0),
        "reb": 4.0 + (6.5 if kind == 2 else 0) + (i % 5) * 0.3,
        "ast": 2.5 + (6.0 if kind == 1 else 0) + (i % 3) * 0.4,
        "stl": 0.7 + (i % 4) * 0.2,
        "blk": 0.3 + (1.6 if kind == 2 else 0),
        "tov": 1.2 + (1.8 if kind == 1 else 0) + (i % 3) * 0.2,
        "fgm": 8.5 - i * 0.1, "fga": 18.0 - i * 0.2 + (2 if kind == 3 else 0),
        "fg3m": 0.6 + (2.6 if kind == 3 else 0), "fg3a": 2.0 + (6.0 if kind == 3 else 0),
        "ftm": 4.0 - i * 0.05, "fta": 5.0 - i * 0.05 + (1.5 if kind == 2 else 0),
        "min": 34.0 - i * 0.3,
    })


def _calendar() -> tuple[SampleWeek, ...]:
    """Twelve weeks of three shared nights; Denver adds a fourth from week 8."""
    def week(number: int) -> SampleWeek:
        days = [frozenset(TEAMS)] * 3 + [frozenset({"DEN"}) if number >= 8 else frozenset()] + [frozenset()] * 3
        return SampleWeek(number, tuple(days))
    return tuple(week(n) for n in range(1, 13))


def _inputs(n: int = 40, *, in_season: bool) -> BoardInputs:
    pool = []
    for i in range(n):
        line = _line(i)
        fpts = round(DEFAULT_POINTS.score(line), 1)
        gp = 58 + (i * 7) % 20
        pool.append(PoolRow(id=100 + i, name=f"P{100 + i}", team=None, gp=gp, line=line,
                            fpts_avg=fpts, fpts_total=fpts * gp))
    ids = [row.id for row in pool]
    inputs = BoardInputs(
        season="2026-27", pool=pool,
        source={pid: "projection" for pid in ids},
        projected_gp={row.id: row.gp for row in pool},
        projections_as_of=date(2026, 10, 31), projection_source="cv",
        current_team={pid: TEAMS[i % 4] for i, pid in enumerate(ids)},
        market={ids[3]: {"injury_status": "OUT"}, ids[4]: {"injury_status": "ACTIVE"}},
        calendar=_calendar(),
    )
    if in_season:
        # Three weeks in: everyone but the last two has played, the fifth is hurt today,
        # and each player's games are what his team has left
        inputs.in_season = draft_in_season.InSeason(
            games_from=TODAY, season_as_of=date(2026, 11, 15),
            season_gp={pid: 8 + i % 5 for i, pid in enumerate(ids[:-2])},
        )
        inputs.games_left = {pid: 30.0 + (i * 3) % 11 for i, pid in enumerate(ids)}
        inputs.live_injury = {ids[4]: "DAY_TO_DAY"}
        inputs.source[ids[-3]] = "season"
    return inputs


def _on_the_live_projection(inputs: BoardInputs, out_until_week: int = 0) -> BoardInputs:
    """`inputs` priced by this morning's live projection: every player at nine
    games in ten of his team's, except the first, who sits out until `out_until_week`."""
    ids = [row.id for row in inputs.pool]
    by_team: dict[str, dict[int, int]] = {}
    for week in inputs.calendar:
        for day in week.days:
            for team in day:
                by_team.setdefault(team, {}).setdefault(week.number, 0)
                by_team[team][week.number] += 1
    inputs.live_games = {
        pid: {week: 0.9 * n * (0.0 if (pid == ids[0] and week < out_until_week) else 1.0)
              for week, n in by_team[inputs.current_team[pid]].items()}
        for pid in ids
    }
    inputs.games_left = {pid: round(sum(weeks.values()), 2) for pid, weeks in inputs.live_games.items()}
    inputs.source = {pid: "live" for pid in ids}
    inputs.in_season.live_as_of, inputs.in_season.live = TODAY, len(ids)
    return inputs


def _room_ranks(inputs: BoardInputs, fmt: str) -> dict[int, int]:
    """The `cv_rank` a league-less room shows for the same inputs."""
    scoring = resolve_scoring(None, "categories" if fmt == "categories" else None)
    resp = DraftBoardService._build_board(
        scoring, frozenset(), frozenset(), inputs,
        BoardSession(board_source="cv", league_size=STANDARD_LEAGUE_SIZE, rounds=STANDARD_ROUNDS),
    )
    return {row.player_id: row.cv_rank for row in resp.data}


@pytest.mark.parametrize("in_season, live", [(True, True), (True, False), (False, False)])
def test_the_stored_ranks_are_the_ones_the_board_shows(in_season, live):
    inputs = _inputs(in_season=in_season)
    if live:
        inputs = _on_the_live_projection(inputs, out_until_week=5)

    boards = daily.build(inputs, TODAY)

    assert {p.player_id: p.points.rank for p in boards.players} == _room_ranks(inputs, "points")
    assert {p.player_id: p.categories.rank for p in boards.players} == _room_ranks(inputs, "categories")
    # Every player once, in points order, and the two boards are separate opinions
    assert [p.points.rank for p in boards.players] == list(range(1, len(inputs.pool) + 1))
    assert sorted(p.categories.rank for p in boards.players) == list(range(1, len(inputs.pool) + 1))
    assert [p.categories.rank for p in boards.players] != [p.points.rank for p in boards.players]


def test_in_season_the_board_is_dated_and_priced_on_what_is_left():
    inputs = _inputs(in_season=True)
    ids = [row.id for row in inputs.pool]

    boards = daily.build(inputs, date(2026, 11, 20))
    by_id = {p.player_id: p for p in boards.players}

    # Dated by the board's own first counted day, not by the caller's clock
    assert (boards.in_season, boards.as_of, boards.model) == (True, TODAY, daily.MODEL_IN_SEASON)
    assert boards.season == "2026-27" and boards.season_as_of == date(2026, 11, 15)
    assert (boards.projections_as_of, boards.projection_source) == (date(2026, 10, 31), "cv")
    assert boards.prior_games == draft_in_season.SEASON_PRIOR_GAMES
    # The games left, on both boards; the projection's full season is gone
    assert by_id[ids[0]].points.games == by_id[ids[0]].categories.games == inputs.games_left[ids[0]]
    # Games played so far, zero for a player who hasn't yet
    assert by_id[ids[0]].season_gp == 8 and by_id[ids[-1]].season_gp == 0
    # Today's report, not the October snapshot's
    assert by_id[ids[4]].injury_status == "DAY_TO_DAY" and by_id[ids[3]].injury_status is None
    assert by_id[ids[-3]].line_source == "season" and by_id[ids[0]].line_source == "projection"
    assert by_id[ids[0]].team == "DEN"


def test_on_the_live_projection_the_board_carries_its_name_and_its_morning():
    inputs = _on_the_live_projection(_inputs(in_season=True))
    boards = daily.build(inputs, TODAY)

    assert (boards.model, boards.live_as_of) == ("three-part-1", TODAY) and boards.model == daily.MODEL_LIVE
    assert {p.line_source for p in boards.players} == {"live"}
    # A Denver player: nine in ten of its 36 shared nights and its 5 extra from week 8
    denver = next(p for p in boards.players if p.player_id == inputs.pool[0].id)
    assert denver.points.games == denver.categories.games == pytest.approx(0.9 * 41, abs=0.01)


def test_a_star_out_for_a_month_is_charged_the_gap_to_a_replacement_not_the_month(monkeypatch):
    # A league small enough for forty players to hold a replacement: two teams of thirteen
    monkeypatch.setattr("services.valuation.standard.STANDARD_LEAGUE_SIZE", 2)

    def rank_of_the_star(live: bool) -> tuple[int, int]:
        inputs = _on_the_live_projection(_inputs(in_season=True), out_until_week=5)
        if not live:
            # The same lines and the same games, valued the way the blend's board values them
            inputs.live_games = {}
            inputs.in_season.live_as_of, inputs.in_season.live = None, 0
        boards = daily.build(inputs, TODAY)
        star = next(p for p in boards.players if p.player_id == inputs.pool[0].id)
        return star.points.rank, star.categories.rank

    live_points, live_categories = rank_of_the_star(live=True)
    plain_points, plain_categories = rank_of_the_star(live=False)
    # Four of his twelve weeks gone: by his own totals he falls most of the way
    # down a forty-player board, by the seat he fills (a replacement plays the
    # month) he stays near the top of it
    assert live_points <= 10 < plain_points
    assert live_categories <= 10 < plain_categories


def test_before_opening_night_it_is_the_projection_board_dated_today():
    inputs = _inputs(in_season=False)
    ids = [row.id for row in inputs.pool]

    boards = daily.build(inputs, TODAY)
    by_id = {p.player_id: p for p in boards.players}

    assert (boards.in_season, boards.as_of, boards.model) == (False, TODAY, daily.MODEL_PRESEASON)
    assert boards.season_as_of is None
    assert all(p.season_gp is None for p in boards.players)
    assert by_id[ids[0]].points.games == float(inputs.pool[0].gp)
    # The market snapshot's flag; a healthy one is no flag
    assert by_id[ids[3]].injury_status == "OUT" and by_id[ids[4]].injury_status is None


def test_values_and_scores_are_the_boards_own_numbers():
    boards = daily.build(_inputs(in_season=True), TODAY)

    top = boards.players[0]
    assert top.points.rank == 1 and top.points.value > 0
    # Points are ordered by fantasy points over the games counted, categories by the summed score
    assert [p.points.score for p in boards.players] == sorted((p.points.score for p in boards.players), reverse=True)
    by_category = sorted(boards.players, key=lambda p: p.categories.rank)
    assert [p.categories.score for p in by_category] == sorted((p.categories.score for p in by_category), reverse=True)
    assert all(p.categories.value >= 0 for p in boards.players)


def test_an_empty_pool_is_an_empty_board():
    boards = daily.build(BoardInputs(season="2026-27", pool=[]), TODAY)
    assert boards.players == [] and boards.in_season is False and boards.as_of == TODAY
