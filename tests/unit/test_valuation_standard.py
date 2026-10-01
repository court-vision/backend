"""
The standard league: the ranks the projections editor shows must be the ranks a
drafter sees, so they are checked against the board itself, not re-derived.
"""

from datetime import date

import pytest

from services.draft_board_service import BoardInputs, BoardSession, DraftBoardService
from services.draft_congestion import SampleWeek
from services.scoring.category_rank import PoolRow
from services.scoring.models import StatLine
from services.scoring.points import DEFAULT_POINTS
from services.scoring.resolver import resolve_scoring
from services.valuation.standard import (
    STANDARD_LEAGUE_SIZE,
    STANDARD_ROUNDS,
    ProjectedLine,
    standard_playoff_weeks,
    standard_ranks,
)

pytestmark = pytest.mark.unit

TEAMS = ("DEN", "BOS", "LAL", "NYK")


def _line(i: int) -> dict[str, float]:
    """A pool with real spread: scorers, passers, bigs and shooters by turns."""
    kind = i % 4
    return {
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
    }


def _lines(n: int = 40) -> list[ProjectedLine]:
    return [
        ProjectedLine(player_id=100 + i, line=_line(i), games=float(58 + (i * 7) % 20),
                      team=TEAMS[i % 4], dd_rate=0.1 * (i % 3), td_rate=0.01 * (i % 2))
        for i in range(n)
    ]


def _calendar() -> tuple[SampleWeek, ...]:
    """Twelve weeks; Denver plays four times a week in the last five, the rest three."""
    def week(number: int) -> SampleWeek:
        days = [frozenset(TEAMS)] * 3 + [frozenset({"DEN"}) if number >= 8 else frozenset()] + [frozenset()] * 3
        return SampleWeek(number, tuple(days))
    return tuple(week(n) for n in range(1, 13))


def _room_ranks(lines, calendar, fmt, weight=2.0) -> dict[int, int]:
    """The `cv_rank` a league-less room shows for the same projections."""
    pool = []
    for p in lines:
        line = StatLine.from_dict(p.line)
        fpts = round(DEFAULT_POINTS.score(line), 1)
        pool.append(PoolRow(id=p.player_id, name=f"P{p.player_id}", team=None, gp=int(p.games),
                            line=line, fpts_avg=fpts, fpts_total=fpts * int(p.games)))
    inputs = BoardInputs(
        season="2026-27", pool=pool,
        source={p.player_id: "projection" for p in lines},
        projected_gp={p.player_id: int(p.games) for p in lines},
        projections_as_of=date(2026, 10, 1), projection_source="cv",
        game_rates={p.player_id: (p.dd_rate, p.td_rate) for p in lines},
        current_team={p.player_id: p.team for p in lines},
        calendar=calendar,
    )
    scoring = resolve_scoring(None, "categories" if fmt == "categories" else None)
    resp = DraftBoardService._build_board(
        scoring, frozenset(), frozenset(), inputs,
        BoardSession(board_source="cv", league_size=STANDARD_LEAGUE_SIZE, rounds=STANDARD_ROUNDS,
                     playoff_weight=weight),
    )
    return {row.player_id: row.cv_rank for row in resp.data}


def test_the_ranks_are_the_ones_a_league_less_room_shows():
    lines, calendar = _lines(), _calendar()
    ranks = standard_ranks(lines, calendar)

    assert [r.player_id for r in ranks] == [p.player_id for p in lines]       # the order sent
    assert {r.player_id: r.points_rank for r in ranks} == _room_ranks(lines, calendar, "points")
    assert {r.player_id: r.category_rank for r in ranks} == _room_ranks(lines, calendar, "categories")
    assert sorted(r.points_rank for r in ranks) == list(range(1, len(lines) + 1))
    assert sorted(r.category_rank for r in ranks) == list(range(1, len(lines) + 1))
    # The two formats are separate opinions, not one list twice.
    assert [r.points_rank for r in ranks] != [r.category_rank for r in ranks]


def test_values_ride_along_with_the_ranks():
    lines = _lines()
    by_id = {r.player_id: r for r in standard_ranks(lines, _calendar())}
    top = min(by_id.values(), key=lambda r: r.points_rank)
    # Fantasy points per game under ESPN's default weights.
    line = StatLine.from_dict(next(p.line for p in lines if p.player_id == top.player_id))
    assert top.points_value == round(DEFAULT_POINTS.score(line), 1)
    assert top.points_season == pytest.approx(top.points_value * top.games, rel=0.01)
    order = sorted(by_id.values(), key=lambda r: r.category_rank)
    assert [r.category_score for r in order] == sorted((r.category_score for r in order), reverse=True)


def test_the_playoff_weight_and_the_calendar_reach_the_ranks():
    """Denver plays more in the playoff weeks, so weighing them moves Nuggets up
    — and with no calendar the weight has nothing to act on."""
    lines, calendar = _lines(), _calendar()
    assert standard_playoff_weeks(calendar) == (8, 9, 10, 11)
    assert standard_playoff_weeks(()) == ()

    flat = {r.player_id: r for r in standard_ranks(lines, calendar, playoff_weight=1.0)}
    heavy = {r.player_id: r for r in standard_ranks(lines, calendar, playoff_weight=4.0)}
    denver = [p.player_id for p in lines if p.team == "DEN"]
    assert all(heavy[pid].games > flat[pid].games for pid in denver)
    assert sum(heavy[pid].points_rank for pid in denver) < sum(flat[pid].points_rank for pid in denver)
    assert {r.player_id: r.points_rank for r in heavy.values()} == _room_ranks(lines, calendar, "points", 4.0)

    bare_flat = standard_ranks(lines, (), playoff_weight=1.0)
    bare_heavy = standard_ranks(lines, (), playoff_weight=4.0)
    assert [r.points_rank for r in bare_flat] == [r.points_rank for r in bare_heavy]


def test_an_edit_to_one_player_moves_him_and_can_move_others():
    """What a preview is: the same pool with one line changed."""
    lines = _lines()
    before = {r.player_id: r for r in standard_ranks(lines, _calendar())}
    target = max(before.values(), key=lambda r: r.points_rank).player_id      # the worst of them
    edited = [
        ProjectedLine(p.player_id, {**p.line, "pts": p.line["pts"] + 25}, 82.0, p.team, p.dd_rate, p.td_rate)
        if p.player_id == target else p
        for p in lines
    ]
    after = {r.player_id: r for r in standard_ranks(edited, _calendar())}
    assert after[target].points_rank < before[target].points_rank
    assert after[target].category_rank < before[target].category_rank
    assert any(after[pid].points_rank > before[pid].points_rank for pid in before if pid != target)


def test_missing_games_are_valued_at_the_default_and_a_duplicate_keeps_its_last_line():
    lines = _lines(12)
    unknown = [ProjectedLine(p.player_id, p.line, None, p.team) for p in lines]
    assert {r.games for r in standard_ranks(unknown)} == {65.0}

    doubled = lines + [ProjectedLine(lines[0].player_id, {**lines[0].line, "pts": 60.0}, 82.0, "DEN")]
    ranks = standard_ranks(doubled)
    assert len(ranks) == len(doubled)
    assert ranks[0].points_rank == ranks[-1].points_rank == 1
