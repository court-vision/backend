"""
Integration: INIT reconciliation against the real schema.

The unit layer covers the pure classification; this exercises the transaction —
the header written onto an empty session, every made pick recorded at its ESPN
number, a second POST skipping them all, conflicts against prior manual picks,
the league-mismatch guard, and that a keeper-shaped row does not break a later
PATCH.
"""

import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from core.errors import ConflictError
from db.models.drafts import DraftPick, DraftSession
from db.models.leagues import League
from db.models.nba.players import Player
from db.models.users import User
from schemas.draft import DraftInitSyncRequest, DraftPickCreate, DraftSessionCreate, DraftSessionUpdate
from db.models.teams import Team
from schemas.common import FantasyProvider, LeagueInfo
from services.draft_adoption_service import DraftAdoptionService
from services.draft_service import DraftService
from services.draft_sync_service import DraftSyncService
from services.team_service import TeamService
from utils.espn_draft_init import decode_init, made_picks, pick_order_of, strip_init_prefix

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ROOMOPEN = strip_init_prefix((FIXTURES / "espn_draft_init_roomopen.b64").read_text())
INPROGRESS = strip_init_prefix((FIXTURES / "espn_draft_init_inprogress_45picks.b64").read_text())
# ESPN's league read (mTeam + mSettings) for the room the INIT fixtures come from.
SETTINGS = json.loads((FIXTURES / "espn_settings_h2h_category.json").read_text())
ROOM_LEAGUE = 35392660
MY_ESPN_TEAM = 5                  # INIT.teamId in the room-open capture


def _league_payload(league_id=ROOM_LEAGUE, teams=8):
    payload = copy.deepcopy(SETTINGS)
    payload["id"] = league_id
    payload["teams"] = [{"id": i, "name": f"Room Team {i}", "abbrev": f"T{i}"} for i in range(1, teams + 1)]
    return payload


@pytest.fixture
def user(integration_db):
    return User.create(email="sync@courtvision.dev", clerk_user_id="user_sync", created_at=datetime.utcnow())


@pytest.fixture
def players(integration_db):
    # The first made pick in the in-progress fixture (team 1) and one of team 4's.
    Player.create(id=100, name="Jokic", name_normalized="jokic", espn_id=3112335, position="C")
    Player.create(id=200, name="Edwards", name_normalized="edwards", espn_id=5104157, position="G")


@pytest.fixture
def espn_league(monkeypatch):
    """Answer the one ESPN read adoption makes, record it, and let a test make it fail."""
    from services import espn_service as module

    state = {"payload": _league_payload(), "error": None, "calls": []}

    async def fake(provider, url, **kwargs):
        state["calls"].append({"provider": provider, "url": url, **kwargs})
        if state["error"] is not None:
            raise state["error"]
        return state["payload"]

    monkeypatch.setattr(module, "provider_get", fake)
    return state


async def _pending_room(user, **overrides):
    """A live room with no team: it adopts whichever ESPN draft the tap connects to."""
    body = dict(kind="live", scoring_format="categories")
    body.update(overrides)
    return (await DraftService.create_session(user.user_id, DraftSessionCreate(**body))).data


async def _init(session_id, user_id, payload=ROOMOPEN):
    """What the route does: reconcile, then let a pending room adopt its league."""
    resp = await DraftSyncService.sync_init(session_id, DraftInitSyncRequest(payload=payload))
    return await DraftAdoptionService.adopt_after_init(session_id, user_id, resp)


async def _mock_session(user, **overrides):
    """A session with no team/league — accepts any ESPN room."""
    body = dict(kind="mock")
    body.update(overrides)
    return (await DraftService.create_session(user.user_id, DraftSessionCreate(**body))).data


def _stored(session_id):
    return list(DraftPick.select().where(DraftPick.session == session_id).order_by(DraftPick.overall_pick))


def _header_of(session_id) -> dict:
    """Every session field an INIT can write — what "the room is unchanged" means."""
    row = DraftSession.get_by_id(session_id)
    return {
        "espn_league_id": row.espn_league_id,
        "pick_order": list(row.pick_order or []),
        "my_slot": row.my_slot,
        "rounds": row.rounds,
        "draft_type": row.draft_type,
    }


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


async def test_a_league_less_room_takes_the_lobbys_round_count(user):
    # The room assumed the standard 13 (or the user typed something); the ESPN
    # lobby it joins is the one authority on how long its draft is.
    session = await _mock_session(user, rounds=12)
    resp = await DraftSyncService.sync_init(session.id, DraftInitSyncRequest(payload=ROOMOPEN))

    assert resp.data.header_applied is True
    assert resp.data.session.rounds == 13 and _header_of(session.id)["rounds"] == 13


async def test_a_league_pending_room_adopts_the_league_the_tap_connects_to(user, espn_league):
    room = await _pending_room(user)
    assert room.league_pending is True and room.team_id is None and room.scoring_format == "categories"

    data = (await _init(room.id, user.user_id)).data

    assert data.header_applied is True and data.session.espn_league_id == ROOM_LEAGUE
    adopted = data.league_adopted
    assert adopted is not None and data.league_adoption_error is None
    assert adopted.espn_team_id == MY_ESPN_TEAM and adopted.team_name == "Room Team 5"
    assert adopted.team_created is True and adopted.league_name == SETTINGS["settings"]["name"]
    # The rows the add-team flow would have written, by its identifier, now attached to the room.
    team = Team.get_by_id(adopted.team_id)
    stored = json.loads(team.league_info)
    assert team.team_identifier == "35392660Room Team 5"
    assert (stored["league_id"], stored["team_name"], stored["espn_team_id"]) == (ROOM_LEAGUE, "Room Team 5", MY_ESPN_TEAM)
    league = League.get_by_id(adopted.league_id)
    assert (league.provider, league.provider_league_id, league.scoring_type) == ("espn", str(ROOM_LEAGUE), "categories")
    assert team.league_id == league.id
    session = DraftSession.get_by_id(room.id)
    assert (session.team_id, session.league_id, session.scoring_format) == (team.team_id, league.id, None)
    assert data.session.league_pending is False and data.session.team_id == team.team_id
    # One read, with the two views the add-team check uses.
    assert len(espn_league["calls"]) == 1 and espn_league["calls"][0]["params"]["view"] == ["mTeam", "mSettings"]

    # A reconnect's INIT finds nothing left to adopt and asks ESPN nothing.
    again = (await _init(room.id, user.user_id)).data
    assert again.league_adopted is None and again.league_adoption_error is None
    assert len(espn_league["calls"]) == 1
    assert Team.select().where(Team.team_identifier == "35392660Room Team 5").count() == 1


async def test_adoption_attaches_a_team_already_on_the_account(user, espn_league):
    # The user got the team added after all; the room takes it rather than duplicating it.
    info = LeagueInfo(provider=FantasyProvider.ESPN, league_id=ROOM_LEAGUE, team_name="Room Team 5", year=2027)
    mine = Team.create(user_id=user.user_id, team_identifier="35392660Room Team 5",
                       league_info=TeamService.serialize_league_info(info))
    room = await _pending_room(user)

    data = (await _init(room.id, user.user_id)).data

    assert data.league_adopted is not None and data.league_adopted.team_created is False
    assert data.league_adopted.team_id == mine.team_id
    assert Team.select().where(Team.team_identifier == "35392660Room Team 5").count() == 1
    assert DraftSession.get_by_id(room.id).team_id == mine.team_id
    # The team had no league yet: adoption synced one from the same read.
    assert Team.get_by_id(mine.team_id).league_id == data.league_adopted.league_id


async def test_a_concurrent_adoption_keeps_the_first_team_and_drops_the_duplicate(user, espn_league):
    # Two INITs (two tabs) both created the team before either attached it: the
    # first attach wins, the loser reports the winner's team and removes its own.
    room = await _pending_room(user)
    first = (await _init(room.id, user.user_id)).data.league_adopted
    duplicate = Team.create(user_id=user.user_id, team_identifier="35392660Room Team 5", league_info="{}")

    team_id, league_id = DraftAdoptionService._attach(room.id, duplicate.team_id, True)

    assert (team_id, league_id) == (first.team_id, first.league_id)
    assert Team.get_or_none(Team.team_id == duplicate.team_id) is None
    assert Team.select().where(Team.team_identifier == "35392660Room Team 5").count() == 1


async def test_adoption_failing_keeps_the_room_pending_and_the_picks_synced(user, espn_league):
    espn_league["error"] = RuntimeError("ESPN is down")
    room = await _pending_room(user)

    data = (await _init(room.id, user.user_id, INPROGRESS)).data

    assert data.inserted == 45                       # the draft itself never waits on ESPN's league read
    assert data.league_adopted is None and "ESPN is down" in (data.league_adoption_error or "")
    session = DraftSession.get_by_id(room.id)
    assert session.espn_league_id is not None and session.team_id is None and session.scoring_format == "categories"
    assert data.session.league_pending is True
    assert Team.select().count() == 0

    # The next connect tries again, and the reconnect's picks are the usual skips.
    espn_league["error"] = None
    espn_league["payload"]["id"] = session.espn_league_id
    again = (await _init(room.id, user.user_id, INPROGRESS)).data
    assert again.league_adopted is not None and again.skipped == 45
    assert DraftSession.get_by_id(room.id).team_id == again.league_adopted.team_id


async def test_an_init_that_names_no_team_of_yours_adopts_nothing(user, espn_league):
    # A spectator's INIT (or a team ESPN no longer lists): the room links to the draft but cannot know whose team you are.
    espn_league["payload"]["teams"] = [t for t in espn_league["payload"]["teams"] if t["id"] != MY_ESPN_TEAM]
    room = await _pending_room(user)

    data = (await _init(room.id, user.user_id)).data

    assert data.session.espn_league_id == ROOM_LEAGUE
    assert data.league_adopted is None and f"team {MY_ESPN_TEAM}" in (data.league_adoption_error or "")
    assert DraftSession.get_by_id(room.id).team_id is None and Team.select().count() == 0


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
    this request's transaction and rolls back with the refusal, so the room ends
    where it began — and "unchanged" is the whole assertion, because this INIT
    would otherwise have left its own league and its own header behind (which is
    exactly what `test_room_open_init_sets_the_header_on_an_empty_session`
    shows it does to a room that is free to take it).
    """
    from services import draft_sync_service as module

    session = await _mock_session(user)
    before = _header_of(session.id)

    real_lock = module.lock_room

    def link_elsewhere_then_lock(session_id):
        DraftSession.update(espn_league_id=424242).where(DraftSession.id == session_id).execute()
        real_lock(session_id)

    monkeypatch.setattr(module, "lock_room", link_elsewhere_then_lock)

    with pytest.raises(ConflictError) as exc:
        await DraftSyncService.sync_init(session.id, DraftInitSyncRequest(payload=ROOMOPEN))
    assert exc.value.error_code == "DRAFT_INIT_LEAGUE_MISMATCH"

    incoming = decode_init(ROOMOPEN)
    assert _header_of(session.id) == before
    assert before["espn_league_id"] != incoming["leagueId"]
    assert before["pick_order"] != pick_order_of(incoming), "the INIT's own header must differ, or this proves nothing"
    assert _stored(session.id) == []
