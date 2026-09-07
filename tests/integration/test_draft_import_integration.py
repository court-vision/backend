"""Importing the captured ESPN draft must reproduce the replay, pick for pick.

`tests/fixtures/espn_draft_detail_4team.json` is ESPN's own payload for the
same 52-pick draft that `draft_replay_4team_13round.json` replays through the
room. One is what the provider said; the other is the ground truth CI already
holds. If the import path is right, the two agree on every row — which is a
real check rather than a tautology, because nothing in the import reads the
replay fixture.
"""

import copy
import json
from datetime import datetime
from pathlib import Path

import pytest

from core.errors import BadRequestError, ConflictError
from db.models.drafts import DraftPick, DraftSession
from db.models.teams import Team
from schemas.common import FantasyProvider, LeagueInfo
from schemas.draft import DraftPickCreate, DraftSessionCreate
from services.draft_import_service import DraftImportService
from services.draft_service import DraftService

from tests.integration.test_draft_replay_integration import (  # noqa: F401  (fixtures)
    MY_SLOT,
    board_players,
    league,
    team,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
DETAIL = json.loads((FIXTURES / "espn_draft_detail_4team.json").read_text())
MY_TEAM = "Replay Team Four"      # ESPN team 4, seat 3 of the captured order


@pytest.fixture
def payload():
    return copy.deepcopy(DETAIL)


@pytest.fixture
def league_info(replay):
    return LeagueInfo(
        provider=FantasyProvider.ESPN, league_id=replay["league_id"],
        team_name=MY_TEAM, year=2027, espn_s2="s2", swid="{SWID}",
    )


@pytest.fixture
def espn(monkeypatch, payload):
    """Answer the one ESPN request the import makes, and record it."""
    from services import draft_import_service as module

    calls = []

    async def fake(provider, url, **kwargs):
        calls.append({"provider": provider, "url": url, **kwargs})
        return payload

    monkeypatch.setattr(module, "provider_get", fake)
    return calls


async def _room(team, kind="import", **overrides):
    body = dict(team_id=team.team_id, kind=kind)
    body.update(overrides)
    return (await DraftService.create_session(team.user_id, DraftSessionCreate(**body))).data


async def test_the_captured_draft_imports_into_the_replay_fixture(team, board_players, replay, league_info, espn):
    session = await _room(team)

    resp = await DraftImportService.import_draft(session.id, league_info)

    assert resp.data.inserted == 52 and resp.data.skipped == 0 and resp.data.conflicts == []
    assert resp.data.header_applied is True and resp.data.espn_team_id == 4

    stored = list(DraftPick.select().where(DraftPick.session == session.id)
                  .order_by(DraftPick.overall_pick))
    order = replay["pick_order"]
    assert [p.overall_pick for p in stored] == [p["overall"] for p in replay["picks"]]
    assert [p.round for p in stored] == [p["round"] for p in replay["picks"]]
    assert [order[p.slot - 1] for p in stored] == [p["team"] for p in replay["picks"]]
    assert [p.espn_player_id for p in stored] == [p["espn_player_id"] for p in replay["picks"]]
    assert [p.player_id for p in stored] == [board_players[p["espn_player_id"]] for p in replay["picks"]]
    assert {p.source for p in stored} == {"import"}
    # Every pick ESPN credited to team 4 is mine, and only those.
    assert [p.overall_pick for p in stored if p.by_me] == [
        p["overall"] for p in replay["picks"] if p["team"] == 4
    ]


async def test_the_import_takes_the_drafts_own_shape_and_closes_the_room(team, board_players, replay, league_info, espn):
    session = await _room(team)

    await DraftImportService.import_draft(session.id, league_info)

    row = DraftSession.get_by_id(session.id)
    assert list(row.pick_order) == replay["pick_order"]
    assert row.rounds == replay["rounds"] and row.draft_type == "snake"
    assert row.my_slot == MY_SLOT
    assert row.status == "completed" and row.completed_at is not None
    assert row.espn_league_id == replay["league_id"]
    assert espn[0]["params"] == {"view": ["mDraftDetail", "mTeam"]}
    assert espn[0]["cookies"] == {"espn_s2": "s2", "SWID": "{SWID}"}
    assert str(replay["league_id"]) in espn[0]["url"]


async def test_importing_twice_records_nothing_the_second_time(team, board_players, replay, league_info, espn):
    session = await _room(team)
    await DraftImportService.import_draft(session.id, league_info)

    again = await DraftImportService.import_draft(session.id, league_info)

    assert again.data.inserted == 0 and again.data.skipped == 52
    assert again.data.conflicts == [] and again.data.warnings == []
    assert DraftPick.select().where(DraftPick.session == session.id).count() == 52


async def test_a_pick_the_room_already_recorded_differently_is_reported_not_overwritten(
    team, board_players, replay, league_info, espn
):
    """The room's own record wins; the import says where they disagree."""
    session = await _room(team)
    # Pick 1 was someone else in this room — a mistyped correction, say.
    await DraftService.add_pick(session.id, DraftPickCreate(
        espn_player_id=replay["picks"][5]["espn_player_id"]
    ))

    resp = await DraftImportService.import_draft(session.id, league_info)

    assert resp.data.inserted == 50   # pick 1 disagrees, pick 6's player is held
    conflicts = {c.reason for c in resp.data.conflicts}
    assert conflicts == {"pick_number_taken", "player_already_drafted"}
    held = DraftPick.get((DraftPick.session == session.id) & (DraftPick.overall_pick == 1))
    assert held.espn_player_id == replay["picks"][5]["espn_player_id"]
    # A session with picks keeps its own header and is told how it differs.
    assert resp.data.header_applied is False


async def test_a_draft_still_running_cannot_be_imported(team, league_info, espn, payload):
    payload["draftDetail"]["drafted"] = False
    payload["draftDetail"]["inProgress"] = True
    session = await _room(team)

    with pytest.raises(ConflictError) as exc:
        await DraftImportService.import_draft(session.id, league_info)

    assert exc.value.error_code == "DRAFT_NOT_COMPLETE"
    assert exc.value.data == {"in_progress": True}
    assert DraftPick.select().where(DraftPick.session == session.id).count() == 0


async def test_a_team_name_the_league_does_not_have_is_a_400(team, league_info, espn):
    session = await _room(team)
    league_info.team_name = "Not My Team"

    with pytest.raises(BadRequestError) as exc:
        await DraftImportService.import_draft(session.id, league_info)

    assert exc.value.error_code == "TEAM_NAME_NOT_IN_LEAGUE"


async def test_a_yahoo_league_is_refused_before_any_request(team, league_info, espn):
    session = await _room(team)
    league_info.provider = FantasyProvider.YAHOO

    with pytest.raises(BadRequestError) as exc:
        await DraftImportService.import_draft(session.id, league_info)

    assert exc.value.error_code == "IMPORT_PROVIDER_UNSUPPORTED"
    assert espn == []


async def test_a_room_holding_simulated_picks_refuses_the_import(team, board_players, replay, league_info, espn):
    """The mirror of the sync's refusal: a room plays a mock or records a real
    draft, never both."""
    session = await _room(team)
    DraftPick.create(session_id=session.id, overall_pick=1, round=1, slot=1,
                     player_id=None, espn_player_id=123, player_name="Simulated",
                     by_me=False, source="mock", created_at=datetime.utcnow())

    with pytest.raises(ConflictError) as exc:
        await DraftImportService.import_draft(session.id, league_info)

    assert exc.value.error_code == "DRAFT_ROOM_IS_SIMULATED"


async def test_a_room_following_a_different_espn_draft_refuses_the_import(team, board_players, league_info, espn):
    session = await _room(team)
    DraftSession.update(espn_league_id=999_999).where(DraftSession.id == session.id).execute()

    with pytest.raises(ConflictError) as exc:
        await DraftImportService.import_draft(session.id, league_info)

    assert exc.value.error_code == "DRAFT_INIT_LEAGUE_MISMATCH"


async def test_espn_keepers_arrive_as_keeper_picks(team, board_players, replay, league_info, espn, payload):
    """The captured league designated none, so this layers keepers onto the real
    picks — another seat's keeper is a spent pick, mine is repriceable."""
    for pick in payload["draftDetail"]["picks"]:
        if pick["overallPickNumber"] in (1, 3):    # pick 3 is seat 3's, i.e. mine
            pick["keeper"] = True
    session = await _room(team, my_slot=MY_SLOT)

    resp = await DraftImportService.import_draft(session.id, league_info)

    keepers = list(DraftPick.select().where(
        (DraftPick.session == session.id) & (DraftPick.source == "keeper")
    ).order_by(DraftPick.overall_pick))
    assert [p.overall_pick for p in keepers] == [1, 3]
    assert [p.by_me for p in keepers] == [False, True]
    # Only my own keeper earns a designation the room can reprice.
    assert [k.espn_player_id for k in resp.data.session.keepers] == [
        p["espn_player_id"] for p in replay["picks"] if p["overall"] == 3
    ]
