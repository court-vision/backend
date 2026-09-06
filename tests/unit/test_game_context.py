"""
Resolving a box-score row to the game it belongs to.

`nba.player_game_stats` has no game id, so the fixture is recovered from
(game_date, team). These tests drive `GameContext` over stub rows and stub
games — the point is the resolution rule, not the query.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from services.game_context import GameContext

pytestmark = pytest.mark.unit


def row(team_id, day=1):
    return SimpleNamespace(team_id=team_id, game_date=date(2026, 11, day))


def game(game_id, home, away, day=1):
    return SimpleNamespace(
        game_id=game_id, home_team_id=home, away_team_id=away, game_date=date(2026, 11, day)
    )


def context_of(games, r):
    """A GameContext built directly from stub games, bypassing the query."""
    by_date_team = {}
    for g in games:
        for team in (g.home_team_id, g.away_team_id):
            by_date_team.setdefault((g.game_date, team), []).append(g)
    return GameContext(by_date_team).of(r)


def test_the_home_side_faces_the_away_team():
    got = context_of([game("0022600001", "LAL", "BOS")], row("LAL"))
    assert got == {"game_id": "0022600001", "home": True, "opponent": "BOS"}


def test_the_away_side_faces_the_home_team():
    got = context_of([game("0022600001", "LAL", "BOS")], row("BOS"))
    assert got == {"game_id": "0022600001", "home": False, "opponent": "LAL"}


def test_a_row_with_no_game_on_that_date_resolves_to_nothing():
    # Not an error: a game missing from nba.games leaves the log without a
    # fixture, and the model's defaults render it absent rather than wrong.
    assert context_of([game("0022600001", "LAL", "BOS", day=2)], row("LAL", day=1)) == {}


def test_an_ambiguous_date_resolves_to_nothing_rather_than_a_guess():
    # Two candidates should be impossible — an NBA team plays once a day — but
    # if the schedule ever says otherwise, an absent opponent costs a label and
    # a wrong one names the wrong team.
    two = [game("0022600001", "LAL", "BOS"), game("0022600002", "LAL", "NYK")]
    assert context_of(two, row("LAL")) == {}


def test_a_row_without_a_team_resolves_to_nothing():
    assert context_of([game("0022600001", "LAL", "BOS")], row(None)) == {}


def test_no_rows_needs_no_query():
    # `for_rows` short-circuits on an empty log, so an empty response does not
    # reach the database at all.
    assert GameContext.for_rows([]).of(row("LAL")) == {}
