"""
The mock autopicker against a real database, in the shape of a real draft.

The league is the replay capture's own — a 4-team, 13-round snake, 52 picks,
seat 3 mine — so the geometry the autopicker fills is the geometry a genuine
ESPN draft had. The *players* are not the capture's: a 52-player pool for a
52-pick draft leaves the last seat one candidate, and "no cap breach" would then
be arithmetic rather than an assertion. A hundred ranked players gives the caps
room to actually bind.

What this guards:

- a mock advanced to the end is the whole draft, once each, my seat included
- no seat exceeds the league's hard centre cap
- the same room replays identically, and a different room does not
- thirteen 'sim to my pick' presses land on the draft one 'sim to end' would
- the rooms the autopicker refuses to touch
- the CV-value fallback, which is the only path a database without a market
  snapshot (every dev database today) can take
"""

from datetime import date, datetime
from pathlib import Path

import pytest

from core.errors import BadRequestError, ConflictError
from core.settings import settings
from db.models.drafts import DraftPick, DraftSession
from db.models.leagues import League
from db.models.nba.draft_market import DraftMarket
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.players import Player
from db.models.teams import Team
from db.models.users import User
from schemas.draft import (
    DraftInitSyncRequest,
    DraftPickCreate,
    DraftSessionCreate,
    DraftSessionUpdate,
    MockAdvanceRequest,
)
from services.draft_mock_service import DraftMockService
from services.draft_service import DraftService, slot_of
from services.draft_sync_service import DraftSyncService
from services.scoring.pool import baseline_season
from utils.espn_draft_init import strip_init_prefix

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
ROOMOPEN = strip_init_prefix((FIXTURES / "espn_draft_init_roomopen.b64").read_text())

ROSTER_SLOTS = {"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "G": 1, "F": 1,
                "UT": 3, "BE": 3, "IR": 1}
POSITION_LIMITS = {"C": 4}          # the plan's cap; four centres of thirteen picks
POSITIONS = ["PG", "SG", "SF", "PF", "C"]
ELIGIBLE = {"PG": [0, 5, 11], "SG": [1, 5, 11], "SF": [2, 6, 11],
            "PF": [3, 6, 11], "C": [4, 11]}
MY_SLOT = 3
POOL_SIZE = 100                     # ranked players for a 52-pick draft: caps can bind


@pytest.fixture
def league(integration_db, replay):
    return League.create(
        provider="espn", provider_league_id=str(replay["league_id"]), season=2027,
        name="Mock League", scoring_type="points", categories=[],
        point_weights={"pts": 1.0, "reb": 1.2, "ast": 1.5, "stl": 3.0, "blk": 3.0, "tov": -1.0},
        matchup_periods={}, roster_slots=ROSTER_SLOTS, position_limits=POSITION_LIMITS,
        draft_settings={
            "type": replay["draft_type"].upper(),
            "pick_order": replay["pick_order"],
            "keeper_count": 0,
        },
        raw_settings={}, settings_synced_at=datetime.utcnow(),
    )


@pytest.fixture
def team(league):
    user = User.create(email="mock@courtvision.dev", clerk_user_id="user_mock",
                       created_at=datetime.utcnow())
    return Team.create(user_id=user.user_id, team_identifier="mock", league_info="{}", league=league)


@pytest.fixture
def pool(integration_db):
    """A hundred ranked players, best first, positions cycling PG..C.

    ADP is the player's own rank, so "best available" reads straight off the id
    and a mock's picks are checkable by eye. Value descends with rank too, so
    the CV-value fallback draws the same queue by a different route.
    """
    ids = []
    season = baseline_season()
    for index in range(POOL_SIZE):
        nba_id = 900_001 + index
        position = POSITIONS[index % len(POSITIONS)]
        Player.create(id=nba_id, espn_id=500_001 + index, name=f"Mock {index + 1:03d}",
                      name_normalized=f"mock {index + 1:03d}", position=position)
        per_game, gp = 40.0 - index * 0.3, 70
        PlayerSeasonStats.create(
            player=nba_id, team=None, as_of_date=date(2025, 4, 10), season=season, gp=gp,
            fpts=int(per_game * gp), pts=int(per_game * gp), reb=gp * 5, ast=gp * 3,
            stl=gp, blk=gp, tov=gp * 2, min=gp * 30,
            fgm=gp * 8, fga=gp * 16, fg3m=gp * 2, fg3a=gp * 5, ftm=gp * 4, fta=gp * 5,
        )
        DraftMarket.create(
            player=nba_id, season=settings.nba_season, source="espn",
            as_of_date=date(2026, 9, 1), overall_rank=index + 1, adp=float(index + 1),
            auction_value=None, default_position_id=POSITIONS.index(position) + 1,
            eligible_slot_ids=ELIGIBLE[position], injury_status=None,
        )
        ids.append(nba_id)
    return ids


async def _room(team, replay, **overrides):
    """A mock room over the captured league's shape, following nothing."""
    body = dict(team_id=team.team_id, kind="mock", my_slot=MY_SLOT)
    body.update(overrides)
    session = (await DraftService.create_session(team.user_id, DraftSessionCreate(**body))).data
    assert session.total_picks == len(replay["picks"]) == 52
    return session


async def _advance(session_id, until="my_turn"):
    return (await DraftMockService.advance(session_id, MockAdvanceRequest(until=until))).data


def _sequence(session_id) -> list[tuple[int, int]]:
    return [
        (p.overall_pick, p.player_id)
        for p in DraftPick.select().where(DraftPick.session == session_id).order_by(DraftPick.overall_pick)
    ]


def _reset(session_id) -> None:
    """Empty the room without going through the API — a fixture step, not a
    behaviour under test. The session id stays the same, which is the point:
    it is in the autopicker's seed."""
    DraftPick.delete().where(DraftPick.session == session_id).execute()
    (DraftSession.update(status="active", completed_at=None, started_at=None)
     .where(DraftSession.id == session_id).execute())


# ---- a whole draft ---------------------------------------------------------


async def test_a_mock_advanced_to_the_end_drafts_every_pick_once(team, pool, replay):
    session = await _room(team, replay)

    data = await _advance(session.id, until="end")

    assert data.picks_made == 52
    assert data.stopped_reason == "end" and data.stopped_at is None
    assert data.completed is True and data.fallback is False
    assert data.market_as_of == date(2026, 9, 1)

    stored = list(DraftPick.select().where(DraftPick.session == session.id))
    assert len(stored) == 52
    assert len({p.player_id for p in stored}) == 52, "a player was drafted twice"
    assert all(p.source == "mock" for p in stored)
    assert all(p.espn_team_id is None for p in stored), "the autopicker is not ESPN saying anything"
    assert all(p.slot == slot_of(p.overall_pick, 4) for p in stored)
    # My seat is played like any other, and its picks are recorded as mine.
    assert {p.overall_pick for p in stored if p.by_me} == {
        n for n in range(1, 53) if slot_of(n, 4) == MY_SLOT
    }

    detail = (await DraftService.get_session(session.id)).data
    assert detail.status == "completed" and detail.completed_at is not None
    assert detail.pick_count == 52 and detail.started_at is not None


async def test_no_seat_exceeds_the_leagues_centre_cap(team, pool, replay):
    """The plan's `{"C": 4}`. A cap is a per-seat fact, so it is counted per
    seat — and the autopicker stops rather than breach one."""
    session = await _room(team, replay)
    await _advance(session.id, until="end")

    primary = {
        m.player_id: m.default_position_id
        for m in DraftMarket.latest_for_season(settings.nba_season)
    }
    centres: dict[int, int] = {}
    for pick in DraftPick.select().where(DraftPick.session == session.id):
        if primary.get(pick.player_id) == POSITIONS.index("C") + 1:
            centres[pick.slot] = centres.get(pick.slot, 0) + 1
    assert centres, "the pool should put centres on some rosters"
    assert max(centres.values()) <= POSITION_LIMITS["C"]


# ---- determinism -----------------------------------------------------------


async def test_the_same_room_replays_the_same_draft(team, pool, replay):
    session = await _room(team, replay)
    await _advance(session.id, until="end")
    first = _sequence(session.id)

    _reset(session.id)
    await _advance(session.id, until="end")

    assert _sequence(session.id) == first


async def test_another_room_drafts_the_same_players_in_a_different_order(team, pool, replay):
    """The session id is in the seed on purpose: it is what makes running a
    second mock worth anything."""
    first = await _room(team, replay)
    await _advance(first.id, until="end")
    second = await _room(team, replay)
    await _advance(second.id, until="end")

    one, two = _sequence(first.id), _sequence(second.id)
    assert one != two
    assert len({pid for _, pid in two}) == 52


async def test_stopping_at_my_turn_lands_on_the_draft_one_call_would_have(team, pool, replay):
    """The property per-pick seeding exists for, through the database: a run
    broken at my turn and resumed is the same draft as a run that was never
    stopped. A per-advance RNG passes every other test here and fails this one.
    """
    session = await _room(team, replay)
    await _advance(session.id, until="end")
    one_shot = _sequence(session.id)

    _reset(session.id)
    first = await _advance(session.id, until="my_turn")
    assert first.picks_made == 2
    await _advance(session.id, until="end")

    assert _sequence(session.id) == one_shot


async def test_my_turn_stops_exactly_on_the_clock(team, pool, replay):
    session = await _room(team, replay)

    data = await _advance(session.id, until="my_turn")

    assert data.picks_made == MY_SLOT - 1 == 2
    assert data.from_pick == 1 and data.stopped_at == MY_SLOT
    assert data.stopped_reason == "my_turn"
    assert data.session.my_next_pick == MY_SLOT and data.session.picks_until_my_turn == 0
    assert not any(p.by_me for p in data.session.picks)
    assert data.completed is False

    # Pressing it again on the clock is a no-op, not another round.
    again = await _advance(session.id, until="my_turn")
    assert again.picks_made == 0 and again.stopped_at == MY_SLOT


async def test_a_mock_finishes_on_sim_to_my_pick_alone(team, pool, replay):
    """The Sep-18 exit criterion. Seat 3's last turn is pick 51 of 52, so the
    button has to run to the end once my turns are behind me — otherwise a mock
    can never be finished the way it is played."""
    session = await _room(team, replay)

    for _ in range(20):
        data = await _advance(session.id, until="my_turn")
        if data.stopped_reason == "end":
            break
        assert data.stopped_at == data.session.my_next_pick
        # On the clock, the room records my pick from the board — best available.
        await DraftService.add_pick(session.id, DraftPickCreate(
            player_id=_first_undrafted(session.id, pool), by_me=True, source="manual"
        ))
    else:
        pytest.fail("the room never finished")

    assert data.completed is True
    detail = (await DraftService.get_session(session.id)).data
    assert detail.pick_count == 52 and detail.status == "completed"
    assert len([p for p in detail.picks if p.by_me]) == 13
    assert len([p for p in detail.picks if p.source == "manual"]) == 13


def _first_undrafted(session_id, pool) -> int:
    drafted = {p.player_id for p in DraftPick.select().where(DraftPick.session == session_id)}
    return next(pid for pid in pool if pid not in drafted)


# ---- the rooms it refuses --------------------------------------------------


async def test_a_manual_room_is_refused(team, pool, replay):
    session = await _room(team, replay, kind="manual")
    with pytest.raises(ConflictError) as exc:
        await _advance(session.id, until="end")
    assert exc.value.error_code == "NOT_A_MOCK"
    assert DraftPick.select().where(DraftPick.session == session.id).count() == 0


async def test_a_mock_room_following_an_espn_draft_is_refused(team, pool, replay):
    """A linked room takes its picks from the lobby; simulating into it writes
    fiction the next INIT collides with on every pick number."""
    session = await _room(team, replay)
    await DraftService.update_session(session.id, DraftSessionUpdate(espn_league_id=99887766))

    with pytest.raises(ConflictError) as exc:
        await _advance(session.id, until="end")
    assert exc.value.error_code == "MOCK_ROOM_IS_LINKED"


async def test_a_completed_room_is_refused(team, pool, replay):
    session = await _room(team, replay)
    await _advance(session.id, until="end")
    with pytest.raises(ConflictError) as exc:
        await _advance(session.id, until="end")
    assert exc.value.error_code == "DRAFT_NOT_ACTIVE"


async def test_a_room_with_no_slot_can_be_watched_but_not_stopped_at(team, pool, replay):
    session = await _room(team, replay, my_slot=None)

    with pytest.raises(BadRequestError) as exc:
        await _advance(session.id, until="my_turn")
    assert exc.value.error_code == "DRAFT_MOCK_NEEDS_SLOT"

    data = await _advance(session.id, until="end")
    assert data.picks_made == 52 and not any(p.by_me for p in data.session.picks)


async def test_a_room_with_no_shape_is_refused(team, pool, replay):
    """No pick order means no seats to play and no end to run to."""
    session = (await DraftService.create_session(
        team.user_id, DraftSessionCreate(kind="mock", pick_order=[], rounds=13)
    )).data
    with pytest.raises(BadRequestError) as exc:
        await _advance(session.id, until="end")
    assert exc.value.error_code == "DRAFT_MOCK_NEEDS_SHAPE"


async def test_a_room_linked_while_the_pool_was_fetched_is_refused_before_anything_is_written(
    team, pool, replay, monkeypatch
):
    """The window the room lock closes.

    The candidate pool is fetched outside the transaction, so a live sync can
    link the room while it runs. Standing in for the racing sync (a genuinely
    concurrent test would need a second connection and would be timing-bound),
    the link is written from inside the pool fetch itself — the same interleaving
    the lock makes impossible. What is asserted is the recheck: the advance sees
    the link and refuses, rather than leaving a room that both follows an ESPN
    draft and holds simulated picks.
    """
    from services import draft_mock_service as module

    session = await _room(team, replay)
    original = module.DraftMockService._pool

    def link_then_fetch(session_id, scoring, drafted):
        DraftSession.update(espn_league_id=99887766).where(
            DraftSession.id == session_id
        ).execute()
        return original(session_id, scoring, drafted)

    monkeypatch.setattr(module.DraftMockService, "_pool", staticmethod(link_then_fetch))

    with pytest.raises(ConflictError) as exc:
        await _advance(session.id, until="end")
    assert exc.value.error_code == "MOCK_ROOM_IS_LINKED"
    assert DraftPick.select().where(DraftPick.session == session.id).count() == 0


async def test_a_simulated_room_refuses_to_start_following_an_espn_draft(team, pool, replay):
    """The mirror of the refusal above: a room plays a simulated draft or
    follows a real one, never both. Without this the numbers are already spent
    and every INIT pick comes back a conflict."""
    session = await _room(team, replay)
    await _advance(session.id, until="my_turn")

    with pytest.raises(ConflictError) as exc:
        await DraftSyncService.sync_init(session.id, DraftInitSyncRequest(payload=ROOMOPEN))
    assert exc.value.error_code == "DRAFT_ROOM_IS_SIMULATED"


# ---- no market snapshot ----------------------------------------------------


async def test_the_seats_fall_back_to_cv_value_without_a_market_snapshot(team, pool, replay):
    """Every dev database's only path: nothing has published an ADP, so the
    autopicker drafts by the board's own ranking and says so."""
    DraftMarket.delete().execute()
    session = await _room(team, replay)

    data = await _advance(session.id, until="end")

    assert data.fallback is True and data.market_as_of is None
    assert data.picks_made == 52 and data.completed is True
    assert len({p.player_id for p in data.session.picks}) == 52
