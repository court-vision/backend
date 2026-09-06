"""
Resolving a box-score row to the game it belongs to.

`nba.player_game_stats` has no game id, so the fixture is recovered from
(game_date, team). These tests drive `GameContext` over stub rows and stub
games — the point is the resolution rule, not the query.
"""

from datetime import date
from types import SimpleNamespace

import pytest

from services import game_context as module
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


# ---- the stored id, once migration 0019 has filled it ----------------------


def row_with_game(game_id, team_id="LAL", day=1):
    r = row(team_id, day)
    r.game_id = game_id
    return r


class _StubGame:
    """Stands in for the `Game` model so `for_rows` can be driven without a
    database. Every `select().where(...)` answers with the same games and counts
    the call, which is how the no-fallback-query claim above is checked."""

    def __init__(self, games):
        self._games = games
        self.queries = 0

    def select(self):
        self.queries += 1
        return self

    def where(self, *_):
        return self

    def __iter__(self):
        return iter(self._games)

    def __getattr__(self, name):
        # `Game.game_id` / `Game.game_date` etc. in the query expressions.
        return _Anything()


class _Anything:
    def in_(self, *_):
        return self

    def __eq__(self, _):
        return self

    def __and__(self, _):
        return self

    def __or__(self, _):
        return self


def context_by_id(games, r):
    """A GameContext built from stub games keyed by id, as `for_rows` does."""
    return GameContext({}, {g.game_id: g for g in games}).of(r)


def test_a_stored_game_id_resolves_without_inferring_anything():
    got = context_by_id([game("0022600001", "LAL", "BOS")], row_with_game("0022600001"))
    assert got == {"game_id": "0022600001", "home": True, "opponent": "BOS"}


def test_the_stored_id_wins_over_a_date_that_would_say_otherwise():
    # The id is the row's identity; the (date, team) join was only ever a
    # stand-in for it, so it must not override the real answer.
    stored = game("0022600001", "LAL", "BOS")
    by_date = {(stored.game_date, "LAL"): [game("0022609999", "LAL", "NYK")]}
    got = GameContext(by_date, {stored.game_id: stored}).of(row_with_game("0022600001"))
    assert got["game_id"] == "0022600001" and got["opponent"] == "BOS"


def test_a_row_without_a_stored_id_still_falls_back_to_the_date():
    # Rows written before 0019, and rows whose game nba.games does not have.
    stored = game("0022600001", "LAL", "BOS")
    by_date = {(stored.game_date, "LAL"): [stored]}
    assert GameContext(by_date, {}).of(row("LAL")) == {
        "game_id": "0022600001",
        "home": True,
        "opponent": "BOS",
    }


def test_a_stored_id_with_no_matching_game_falls_back_rather_than_failing(monkeypatch):
    """Through `for_rows`, not a hand-built context.

    A dangling id has to be *selected into* the fallback batch, or `of` reaches
    for a candidate list that was never loaded and answers nothing. Building the
    context by hand hides that, which is how it was missed the first time.
    """
    stored = game("0022600001", "LAL", "BOS")
    monkeypatch.setattr(module, "Game", _StubGame([stored]))

    ctx = GameContext.for_rows([row_with_game("0022600404")])

    assert ctx.of(row_with_game("0022600404")) == {
        "game_id": "0022600001",
        "home": True,
        "opponent": "BOS",
    }


def test_for_rows_does_not_load_the_fallback_for_a_fully_resolved_log(monkeypatch):
    """The point of storing the id: a backfilled table stops paying the join."""
    stored = game("0022600001", "LAL", "BOS")
    stub = _StubGame([stored])
    monkeypatch.setattr(module, "Game", stub)

    GameContext.for_rows([row_with_game("0022600001")])

    assert stub.queries == 1, "a second query means the date fallback ran anyway"
