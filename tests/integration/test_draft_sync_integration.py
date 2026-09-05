"""
Integration: INIT reconciliation against the real schema.

The unit layer covers the pure classification; this exercises the transaction —
the header written onto an empty session, every made pick recorded at its ESPN
number, a second POST skipping them all, conflicts against prior manual picks,
the league-mismatch guard, and that a keeper-shaped row does not break a later
PATCH.
"""

from datetime import datetime
from pathlib import Path

import pytest

from core.errors import ConflictError
from db.models.drafts import DraftPick, DraftSession
from db.models.leagues import League
from db.models.nba.players import Player
from db.models.users import User
from schemas.draft import DraftInitSyncRequest, DraftPickCreate, DraftSessionCreate, DraftSessionUpdate
from services.draft_service import DraftService
from services.draft_sync_service import DraftSyncService
from utils.espn_draft_init import decode_init, made_picks, strip_init_prefix

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ROOMOPEN = strip_init_prefix((FIXTURES / "espn_draft_init_roomopen.b64").read_text())
INPROGRESS = strip_init_prefix((FIXTURES / "espn_draft_init_inprogress_45picks.b64").read_text())


@pytest.fixture
def user(integration_db):
    return User.create(email="sync@courtvision.dev", clerk_user_id="user_sync", created_at=datetime.utcnow())


@pytest.fixture
def players(integration_db):
    # The first made pick in the in-progress fixture (team 1) and one of team 4's.
    Player.create(id=100, name="Jokic", name_normalized="jokic", espn_id=3112335, position="C")
    Player.create(id=200, name="Edwards", name_normalized="edwards", espn_id=5104157, position="G")


async def _mock_session(user, **overrides):
    """A session with no team/league — accepts any ESPN room."""
    body = dict(kind="mock")
    body.update(overrides)
    return (await DraftService.create_session(user.user_id, DraftSessionCreate(**body))).data


def _stored(session_id):
    return list(DraftPick.select().where(DraftPick.session == session_id).order_by(DraftPick.overall_pick))


async def test_room_open_init_sets_the_header_on_an_empty_session(user):
    session = await _mock_session(user)
    resp = await DraftSyncService.sync_init(session.id, DraftInitSyncRequest(payload=ROOMOPEN))
    data = resp.data

    assert data.header_applied is True
    assert data.made == 0 and data.inserted == 0
    assert data.session.pick_order == [1, 2, 3, 4, 5, 6, 7, 8]
    assert data.session.my_slot == 5
    assert data.session.rounds == 13
    assert data.espn_league_id == 35392660 and data.espn_team_id == 5
    assert _stored(session.id) == []


async def test_in_progress_init_records_every_made_pick_once(user, players):
    session = await _mock_session(user)
    resp = await DraftSyncService.sync_init(session.id, DraftInitSyncRequest(payload=INPROGRESS))

    assert resp.data.inserted == 45 and resp.data.skipped == 0
    stored = _stored(session.id)
    assert len(stored) == 45
    # Pick 1 resolved to the NBA player, with snake geometry (round 1, slot 1).
    first = stored[0]
    assert first.overall_pick == 1 and first.player_id == 100
    assert first.round == 1 and first.slot == 1 and first.source == "espn_sync"
    # by_me is set for the connecting team (seat 4), and only that seat — every
    # by_me pick lands on slot 4 of the 8-team snake.
    mine = [p for p in stored if p.by_me]
    assert mine and all(p.slot == 4 for p in mine)
    assert all(p.slot != 4 for p in stored if not p.by_me)

    # A second INIT (a reconnect) is a no-op: everything is skipped.
    again = await DraftSyncService.sync_init(session.id, DraftInitSyncRequest(payload=INPROGRESS))
    assert again.data.inserted == 0 and again.data.skipped == 45
    assert len(_stored(session.id)) == 45


async def test_a_prior_manual_pick_at_the_same_number_is_a_conflict(user, players):
    session = await _mock_session(user, pick_order=[1, 2, 3, 4, 5, 6, 7, 8], rounds=13)
    # Record a different player at pick 1 first.
    Player.create(id=999, name="Someone", name_normalized="someone", espn_id=888, position="F")
    await DraftService.add_pick(session.id, DraftPickCreate(player_id=999, overall_pick=1))

    resp = await DraftSyncService.sync_init(session.id, DraftInitSyncRequest(payload=INPROGRESS))
    taken = [c for c in resp.data.conflicts if c.reason == "pick_number_taken"]
    assert any(c.pick_number == 1 for c in taken)
    # The manual pick is untouched; the other 44 land.
    assert _stored(session.id)[0].player_id == 999
    assert resp.data.inserted == 44


async def test_a_prior_manual_pick_of_a_synced_player_is_player_already_drafted(user, players):
    session = await _mock_session(user, pick_order=[1, 2, 3, 4, 5, 6, 7, 8], rounds=13)
    # Jokic (espn 3112335, INIT pick 1) recorded manually at pick 7.
    await DraftService.add_pick(session.id, DraftPickCreate(player_id=100, overall_pick=7))

    resp = await DraftSyncService.sync_init(session.id, DraftInitSyncRequest(payload=INPROGRESS))
    dup = [c for c in resp.data.conflicts if c.reason == "player_already_drafted" and c.espn_player_id == 3112335]
    assert dup and dup[0].held_at == 7


async def test_a_linked_room_refuses_any_other_espn_draft_and_writes_nothing(user):
    session = await _mock_session(user)
    await DraftService.update_session(session.id, DraftSessionUpdate(espn_league_id=999))

    with pytest.raises(ConflictError) as exc:
        await DraftSyncService.sync_init(session.id, DraftInitSyncRequest(payload=ROOMOPEN))
    assert exc.value.error_code == "DRAFT_INIT_LEAGUE_MISMATCH"
    assert _stored(session.id) == []
    assert DraftSession.get_by_id(session.id).espn_league_id == 999


async def test_a_mock_room_links_to_the_first_room_it_reconciles_with_exclusively(user):
    first = await _mock_session(user)
    data = (await DraftSyncService.sync_init(first.id, DraftInitSyncRequest(payload=ROOMOPEN))).data
    assert data.session.espn_league_id == 35392660
    # A reconnect posts the same INIT again: the same room, no conflict.
    again = (await DraftSyncService.sync_init(first.id, DraftInitSyncRequest(payload=ROOMOPEN))).data
    assert again.session.espn_league_id == 35392660

    second = await _mock_session(user)
    with pytest.raises(ConflictError) as exc:
        await DraftSyncService.sync_init(second.id, DraftInitSyncRequest(payload=ROOMOPEN))
    assert exc.value.error_code == "DRAFT_ROOM_ALREADY_LINKED"
    assert str(first.id) in exc.value.message
    assert DraftSession.get_by_id(second.id).espn_league_id is None
    assert _stored(second.id) == []


async def test_a_synced_keeper_does_not_break_a_later_patch(user, players):
    session = await _mock_session(user, pick_order=[1, 2, 3, 4, 5, 6, 7, 8], rounds=13)
    # A keeper from another seat: source keeper, by_me False, no designation.
    DraftPick.create(session_id=session.id, overall_pick=2, player_id=200, source="keeper", by_me=False, round=1, slot=2)

    # A PATCH that touches the session must not try to reprice that keeper.
    resp = await DraftService.update_session(session.id, DraftSessionUpdate(status="completed"))
    assert resp.data.status == "completed"


async def test_synced_picks_carry_the_espn_team_and_its_seat(user, players):
    session = await _mock_session(user)
    data = (await DraftSyncService.sync_init(session.id, DraftInitSyncRequest(payload=INPROGRESS))).data
    order = data.session.pick_order
    stored = _stored(session.id)
    assert stored and all(p.espn_team_id is not None for p in stored)
    assert all(p.slot == order.index(p.espn_team_id) + 1 for p in stored)
    assert stored[0].espn_team_id == 1


async def test_an_init_racing_another_that_linked_the_room_first_is_refused(user, players, monkeypatch):
    """Two INITs can both read the same unlinked room. Whichever takes the row
    first decides what draft it follows; the other must see that and refuse,
    rather than relinking the room — header and all — from what it read before.

    Standing in for the racing request (a real one needs a second connection and
    is timing-bound), the link is written from inside `lock_room` itself: the
    moment the second request's decision goes stale. That stand-in write shares
    this request's transaction and rolls back with the refusal, so what is
    asserted afterwards is that the room did not take *this* INIT's draft — the
    relink the stale decision would have performed.
    """
    from services import draft_sync_service as module

    session = await _mock_session(user)
    real_lock = module.lock_room

    def link_elsewhere_then_lock(session_id):
        DraftSession.update(espn_league_id=424242).where(DraftSession.id == session_id).execute()
        real_lock(session_id)

    monkeypatch.setattr(module, "lock_room", link_elsewhere_then_lock)

    with pytest.raises(ConflictError) as exc:
        await DraftSyncService.sync_init(session.id, DraftInitSyncRequest(payload=ROOMOPEN))
    assert exc.value.error_code == "DRAFT_INIT_LEAGUE_MISMATCH"

    # Nothing from this INIT reached the room: not its league, not its header.
    incoming = decode_init(ROOMOPEN)["leagueId"]
    stored = DraftSession.get_by_id(session.id)
    assert stored.espn_league_id != incoming
    assert stored.pick_order == [] and _stored(session.id) == []
