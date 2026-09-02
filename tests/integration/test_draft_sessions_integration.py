"""
Integration: the draft room's write paths against the real schema.

The unit layer states the rules; this is where the unique indexes and the
transactions actually decide — one pick per player per session, a PATCH that
reshapes the draft re-deriving every pick's round and slot (or refusing to
leave the session inconsistent), and a pick recorded before its player reached
nba.players counting as his everywhere once he has.
"""

from datetime import datetime

import pytest
from peewee import IntegrityError

from core.errors import BadRequestError, ConflictError
from db.models.drafts import DraftPick, DraftSession
from db.models.nba.players import Player
from db.models.users import User
from schemas.draft import DraftPickCreate, DraftSessionCreate, DraftSessionUpdate
from services.draft_board_service import DraftBoardService
from services.draft_service import DraftService

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture
def user(integration_db):
    return User.create(email="draft@courtvision.dev", clerk_user_id="user_draft", created_at=datetime.utcnow())


@pytest.fixture
def players(integration_db):
    Player.create(id=1, name="Star", name_normalized="star", espn_id=101, position="C")
    Player.create(id=2, name="Guard", name_normalized="guard", espn_id=102, position="G")


async def _session(user, **overrides):
    """A four-team snake, 13 rounds (52 picks), the caller in seat 3."""
    body = dict(pick_order=[10, 6, 5, 8], my_slot=3, rounds=13)
    body.update(overrides)
    return (await DraftService.create_session(user.user_id, DraftSessionCreate(**body))).data


def _stored(session_id):
    return list(DraftPick.select().where(DraftPick.session == session_id).order_by(DraftPick.overall_pick))


async def test_the_same_player_is_recorded_once_per_session(user, players):
    session = await _session(user)
    await DraftService.add_pick(session.id, DraftPickCreate(player_id=1, by_me=True))

    with pytest.raises(ConflictError) as exc:
        await DraftService.add_pick(session.id, DraftPickCreate(player_id=1, overall_pick=7))
    assert exc.value.error_code == "DRAFT_PLAYER_ALREADY_DRAFTED"

    # The earlier pick may predate the player: it went in off an ESPN id while
    # nba.players had no such row. Once it does, the resolved player collides.
    await DraftService.add_pick(session.id, DraftPickCreate(espn_player_id=109, player_name="Rookie"))
    Player.create(id=9, name="Rookie", name_normalized="rookie", espn_id=109, position="F")
    with pytest.raises(ConflictError):
        await DraftService.add_pick(session.id, DraftPickCreate(player_id=9))
    with pytest.raises(ConflictError):
        await DraftService.add_pick(session.id, DraftPickCreate(player_name=" rookie "))

    assert [p.overall_pick for p in _stored(session.id)] == [1, 2]


async def test_the_partial_index_settles_the_race_the_service_read_cannot_see(user, players):
    """The service checks before it writes; when two writes race, the index on
    (session_id, player_id) decides. Written behind the service's back to prove
    it fires — and that unresolved picks, having no player_id, sit outside it."""
    session = await _session(user)
    DraftPick.create(session_id=session.id, overall_pick=1, player_id=1)

    with pytest.raises(IntegrityError) as exc:
        DraftPick.create(session_id=session.id, overall_pick=2, player_id=1)
    assert "draft_picks_session_player_uq" in str(exc.value)
    # ...and the service reads that exact failure as the player conflict, not the number one.
    assert DraftService._pick_conflict(exc.value, 2, "Star").error_code == "DRAFT_PLAYER_ALREADY_DRAFTED"

    DraftPick.create(session_id=session.id, overall_pick=3, espn_player_id=555, player_name="Lagging")
    DraftPick.create(session_id=session.id, overall_pick=4, espn_player_id=555, player_name="Lagging")


async def test_reshaping_the_draft_rederives_every_recorded_pick(user, players):
    session = await _session(user)
    await DraftService.add_pick(session.id, DraftPickCreate(player_id=1))
    await DraftService.add_pick(session.id, DraftPickCreate(player_id=2))
    for name in ("Third", "Fourth", "Fifth"):
        await DraftService.add_pick(session.id, DraftPickCreate(player_name=name))
    assert [(p.round, p.slot) for p in _stored(session.id)] == [(1, 1), (1, 2), (1, 3), (1, 4), (2, 4)]

    # An auction has no rounds or slots — for the picks already made either.
    resp = await DraftService.update_session(session.id, DraftSessionUpdate(draft_type="auction"))
    assert [(p.round, p.slot) for p in resp.data.picks] == [(None, None)] * 5
    assert [(p.round, p.slot) for p in _stored(session.id)] == [(None, None)] * 5

    # Back to a snake with a fifth seat: pick 5 now closes round one instead of opening round two.
    resp = await DraftService.update_session(
        session.id, DraftSessionUpdate(draft_type="snake", pick_order=[10, 6, 5, 8, 2])
    )
    expected = [(1, 1), (1, 2), (1, 3), (1, 4), (1, 5)]
    assert [(p.round, p.slot) for p in resp.data.picks] == expected
    assert [(p.round, p.slot) for p in _stored(session.id)] == expected

    # A change that does not touch the geometry leaves the picks alone.
    await DraftService.update_session(session.id, DraftSessionUpdate(status="completed"))
    assert [(p.round, p.slot) for p in _stored(session.id)] == expected


async def test_a_patch_that_would_leave_the_session_inconsistent_is_refused_whole(user, players):
    session = await _session(user)                       # seat 3 of 4; 13 rounds = 52 picks
    await DraftService.add_pick(session.id, DraftPickCreate(player_id=1, overall_pick=30))

    # Shrinking the order under the confirmed slot, with no my_slot in the request.
    with pytest.raises(BadRequestError) as exc:
        await DraftService.update_session(session.id, DraftSessionUpdate(pick_order=[10, 6]))
    assert exc.value.error_code == "DRAFT_SLOT_OUT_OF_RANGE"

    # Shortening the draft below a pick already in it: 4 x 7 = 28 < 30.
    with pytest.raises(BadRequestError) as exc:
        await DraftService.update_session(session.id, DraftSessionUpdate(rounds=7))
    assert exc.value.error_code == "DRAFT_SHORTER_THAN_PICKS"

    # Neither wrote anything: header and pick are exactly as they were.
    stored = DraftSession.get_by_id(session.id)
    assert (stored.pick_order, stored.rounds, stored.my_slot) == ([10, 6, 5, 8], 13, 3)
    assert [(p.overall_pick, p.round, p.slot) for p in _stored(session.id)] == [(30, 8, 3)]


async def test_a_pick_recorded_before_its_player_synced_is_his_once_he_has(user, players):
    session = await _session(user)
    await DraftService.add_pick(
        session.id, DraftPickCreate(espn_player_id=109, player_name="Rookie", by_me=True)
    )
    assert _stored(session.id)[0].player_id is None      # provider identity only

    # Before the sync the board cannot count him; after it he is drafted, and mine.
    before = DraftBoardService._fetch_inputs(frozenset(), session.id)
    assert before.session_picked == frozenset()
    Player.create(id=9, name="Rookie", name_normalized="rookie", espn_id=109, position="F")
    after = DraftBoardService._fetch_inputs(frozenset(), session.id)
    assert after.session_picked == frozenset({9}) and after.session_mine == frozenset({9})
    assert after.positions.get(9) == "F"                 # fetched for the cap check

    # The session detail says the same — and the row itself was not rewritten.
    detail = await DraftService.get_session(session.id)
    assert detail.data.picks[0].player_id == 9
    assert _stored(session.id)[0].player_id is None
