"""The recap over a real draft: the captured 52 picks, graded.

Reuses the replay league's fixtures, so nothing here invents a draft — the
picks are the ones ESPN recorded (`tests/fixtures/draft_replay_4team_13round.json`)
and the seats are the ones that made them.

The capture's board is deliberately monotone (value descends with draft order),
which makes the plain replay a *par* draft: every seat took exactly the player
CV ranked at its pick. That is the property worth pinning first — a recap that
disagrees with the board it was drafted from is broken — and the seats are
separated afterwards by moving two players.
"""

import copy

import pytest

from api import deps
from db.models.leagues import League
from schemas.draft import DraftPickCreate
from services.draft_board_service import BoardSession, DraftBoardService
from services.draft_recap_service import DraftRecapService
from services.draft_service import DraftService
from services.scoring.category_value import default_category_defs
from services.scoring.resolver import resolve_scoring

from tests.integration.test_draft_replay_integration import (  # noqa: F401  (fixtures)
    MY_SLOT,
    _open_room,
    _record,
    board_players,
    league,
    team,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _context(session_id, user_id):
    """The route's own dependency, run against the database."""
    return deps._owned_session(session_id, user_id)


async def _recap(session_id, user_id):
    ctx = _context(session_id, user_id)
    return await DraftRecapService.get_recap(resolve_scoring(ctx.league), ctx)


async def _replay_all(team, replay, board_players, picks=None):
    session = await _open_room(team, replay)
    for pick in picks or replay["picks"]:
        await _record(session.id, pick, board_players, replay)
    return session


async def test_the_captured_draft_recaps_pick_for_pick(team, board_players, replay):
    session = await _replay_all(team, replay, board_players)

    recap = await _recap(session.id, team.user_id)

    assert [p.overall_pick for p in recap.data] == [p["overall"] for p in replay["picks"]]
    assert [p.round for p in recap.data] == [p["round"] for p in replay["picks"]]
    order = replay["pick_order"]
    assert [order[p.slot - 1] for p in recap.data] == [p["team"] for p in replay["picks"]]
    assert [p.player_id for p in recap.data] == [
        board_players[p["espn_player_id"]] for p in replay["picks"]
    ]
    assert recap.meta.picks_made == len(replay["picks"])
    assert recap.meta.unscored == 0 and recap.meta.unattributed == 0
    assert recap.meta.league_size == 4 and recap.meta.total_picks == 52
    assert recap.meta.graded_by == "value_over_slot"


async def test_the_recap_prices_picks_against_the_same_board_the_room_showed(team, board_players, replay):
    """`cv_rank` and `value` must be the board's, not a second opinion."""
    session = await _replay_all(team, replay, board_players)
    ctx = _context(session.id, team.user_id)
    scoring = resolve_scoring(ctx.league)

    board = await DraftBoardService.get_board(scoring, session=BoardSession.of(ctx))
    recap = await DraftRecapService.get_recap(scoring, ctx)

    # Every drafted player is off the board by now, so compare against the
    # roster the board reports for the caller.
    on_board = {row.player_id: row for row in board.roster}
    mine = [p for p in recap.data if p.by_me]
    assert mine and all(p.player_id in on_board for p in mine)
    for pick in mine:
        assert pick.value == on_board[pick.player_id].value


async def test_a_draft_that_took_the_board_in_order_grades_every_seat_the_same(team, board_players, replay):
    """The capture's value ladder descends with draft order, so every pick is
    exactly par — and no seat may be told it drafted better than another."""
    session = await _replay_all(team, replay, board_players)

    recap = await _recap(session.id, team.user_id)

    assert all(p.value_over_slot == 0.0 for p in recap.data)
    assert [s.slot for s in recap.seats] == [1, 2, 3, 4]
    assert {s.value_over_slot for s in recap.seats} == {0.0}
    assert len({s.grade for s in recap.seats}) == 1
    assert [s.picks for s in recap.seats] == [13, 13, 13, 13]
    assert [s.is_me for s in recap.seats] == [False, False, True, False]


async def test_a_reach_and_a_steal_separate_the_seats(team, board_players, replay):
    """Move the best player in the capture to the last pick and the worst to
    the first: two seats now sit either side of the board."""
    picks = copy.deepcopy(replay["picks"])
    first, last = picks[0]["espn_player_id"], picks[-1]["espn_player_id"]
    picks[0]["espn_player_id"], picks[-1]["espn_player_id"] = last, first

    session = await _replay_all(team, replay, board_players, picks=picks)
    recap = await _recap(session.id, team.user_id)

    by_slot = {seat.slot: seat for seat in recap.seats}
    assert by_slot[1].value_over_slot < 0 and by_slot[4].value_over_slot > 0
    assert by_slot[1].worst_pick == 1 and by_slot[4].best_pick == 52
    assert [by_slot[s].grade for s in (1, 2, 3, 4)] == ["D", "B", "B", "A"]
    # The two untouched seats drafted par and are told so.
    assert by_slot[2].value_over_slot == by_slot[3].value_over_slot == 0.0


async def test_an_unresolved_pick_is_graded_around_never_dropped(team, board_players, replay):
    """A pick recorded before its player reached `nba.players` — the lag
    `usr.draft_picks` was shaped to carry."""
    session = await _replay_all(team, replay, board_players, picks=replay["picks"][:8])
    await DraftService.add_pick(
        session.id, DraftPickCreate(espn_player_id=987654, player_name="Not In The Pool")
    )

    recap = await _recap(session.id, team.user_id)

    lagging = recap.data[-1]
    assert lagging.overall_pick == 9 and lagging.player_id is None
    assert lagging.value is None and lagging.value_over_slot is None
    assert lagging.player_name == "Not In The Pool"
    assert recap.meta.unscored == 1
    charged = next(s for s in recap.seats if s.slot == lagging.slot)
    assert charged.unscored == 1 and charged.picks > charged.unscored
    assert recap.meta.complete is False


async def test_a_category_league_projects_roto_standings_from_the_same_draft(team, board_players, replay, league):
    """Same 52 picks, scored as a 9-cat league: every category is ranked and
    the roto pot is spent exactly."""
    League.update(
        scoring_type="categories",
        categories=[c.to_json() for c in default_category_defs()],
    ).where(League.id == league.id).execute()

    session = await _replay_all(team, replay, board_players)
    recap = await _recap(session.id, team.user_id)

    assert recap.meta.format == "categories" and recap.meta.standings_basis == "z_sum"
    assert recap.meta.value_kind == "cat_value"
    assert len(recap.standings) == 4
    for index, category in enumerate(recap.meta.categories):
        awarded = sum(s.categories[index].roto_points for s in recap.standings)
        assert awarded == 4 * 5 / 2, category.key
    for standing in recap.standings:
        assert len(standing.h2h) == 3
        assert all(c.won + c.lost + c.tied == len(recap.meta.categories) for c in standing.h2h)
        assert standing.expected_wins is not None
        assert standing.season_value is None
    assert sum(s.roto_points for s in recap.standings) == len(recap.meta.categories) * 10


async def test_a_points_league_projects_season_value_per_seat(team, board_players, replay):
    session = await _replay_all(team, replay, board_players)

    recap = await _recap(session.id, team.user_id)

    assert recap.meta.standings_basis == "season_value"
    assert [s.slot for s in recap.standings] == [1, 2, 3, 4]
    assert all(s.season_value and s.season_value > 0 for s in recap.standings)
    assert all(s.categories == [] and s.roto_points is None for s in recap.standings)
    # Seat 1 drafted first overall in a snake, so it holds the most value.
    assert max(recap.standings, key=lambda s: s.season_value).slot == 1


async def test_a_room_with_no_picks_answers_rather_than_failing(team, replay, board_players):
    session = await _open_room(team, replay)

    recap = await _recap(session.id, team.user_id)

    assert recap.data == [] and recap.seats == [] and recap.standings == []
    assert recap.meta.picks_made == 0 and recap.meta.complete is False
    assert recap.message == "No picks recorded yet"
