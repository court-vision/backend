"""
End-to-end replay of a real ESPN draft — the plan's Sep-14 exit criterion
("manually replay the disposable-league draft, 52 picks, with undo and
keepers"), automated so it re-runs on every change instead of once.

The capture is `tests/fixtures/draft_replay_4team_13round.json`: 52 picks of a
genuine 4-team, 13-round snake, with the league's own pick order. Nothing here
asserts what we think a draft looks like — it asserts what one did.

What this guards, in the order the bugs actually appeared:

- the seat on the clock for all 52 picks (also checked without a database in
  tests/unit/test_draft_replay.py — this file adds the recorded rows)
- the front after an undo mid-draft: the hole is re-fillable, but the draft has
  not gone backwards
- one player, one pick
- keepers as picks spent before the start: off the board, never the front
- the board and roster tracking the record, with caps counted on ESPN's
  primary position

The captured league designated no keepers (`keeperCount: 0`), so the keeper
cases are layered on top of the real pick sequence and say so.
"""

from datetime import date, datetime

import pytest

from core.errors import BadRequestError, ConflictError
from db.models.drafts import DraftPick
from db.models.leagues import League
from db.models.nba.draft_market import DraftMarket
from db.models.nba.player_season_stats import PlayerSeasonStats
from db.models.nba.players import Player
from db.models.teams import Team
from db.models.users import User
from core.settings import settings
from schemas.draft import DraftPickCreate, DraftSessionCreate, DraftSessionUpdate
from services.draft_board_service import BoardSession, DraftBoardService
from services.draft_service import DraftService
from services.scoring.pool import baseline_season
from services.scoring.resolver import resolve_scoring

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

# The ESPN H2H roster: 13 draftable slots, which is exactly the captured draft's
# 13 rounds — the league's own shape, so `rounds` prefills to the real number.
ROSTER_SLOTS = {"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "G": 1, "F": 1,
                "UT": 3, "BE": 3, "IR": 1}
POSITION_LIMITS = {"C": 3}
POSITIONS = ["PG", "SG", "SF", "PF", "C"]
ELIGIBLE = {"PG": [0, 5, 11], "SG": [1, 5, 11], "SF": [2, 6, 11],
            "PF": [3, 6, 11], "C": [4, 11]}
MY_SLOT = 3        # seat 3 of the captured pick order is ESPN team 4


@pytest.fixture
def league(integration_db, replay):
    """The captured league: its real draft settings, a standard ESPN roster."""
    return League.create(
        provider="espn", provider_league_id=str(replay["league_id"]), season=2027,
        name="Replay League", scoring_type="points", categories=[],
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
    user = User.create(email="replay@courtvision.dev", clerk_user_id="user_replay",
                       created_at=datetime.utcnow())
    return Team.create(user_id=user.user_id, team_identifier="replay", league_info="{}", league=league)


@pytest.fixture
def board_players(integration_db, replay):
    """Every drafted player, valued and ranked, so the board is not empty.

    Value descends with draft order, which makes `cv_rank` meaningful and the
    board's ordering checkable; positions cycle so the roster fills real lineup
    slots and the centre cap is reachable.
    """
    by_espn: dict[int, int] = {}
    season = baseline_season()
    for index, pick in enumerate(replay["picks"]):
        nba_id = 900_001 + index
        espn_id = pick["espn_player_id"]
        position = POSITIONS[index % len(POSITIONS)]
        Player.create(id=nba_id, espn_id=espn_id, name=f"Replay {pick['overall']:02d}",
                      name_normalized=f"replay {pick['overall']:02d}", position=position)
        per_game = 30.0 - index * 0.4          # 30.0 down to 9.6
        gp = 70
        PlayerSeasonStats.create(
            player=nba_id, team=None, as_of_date=date(2025, 4, 10), season=season, gp=gp,
            fpts=int(per_game * gp), pts=int(per_game * gp), reb=gp * 5, ast=gp * 3,
            stl=gp, blk=gp, tov=gp * 2, min=gp * 30,
            fgm=gp * 8, fga=gp * 16, fg3m=gp * 2, fg3a=gp * 5, ftm=gp * 4, fta=gp * 5,
        )
        DraftMarket.create(
            player=nba_id, season=settings.nba_season, source="espn",
            as_of_date=date(2026, 9, 1), overall_rank=pick["overall"],
            adp=float(pick["overall"]), auction_value=None,
            default_position_id=POSITIONS.index(position) + 1,
            eligible_slot_ids=ELIGIBLE[position], injury_status=None,
        )
        by_espn[espn_id] = nba_id
    return by_espn


async def _open_room(team, replay):
    """A room for seat 3, built the way production builds one: everything but
    the slot comes from the league's synced settings."""
    resp = await DraftService.create_session(
        team.user_id, DraftSessionCreate(team_id=team.team_id, my_slot=MY_SLOT)
    )
    session = resp.data
    # The captured draft's own shape, prefilled — not values this test chose.
    assert session.pick_order == replay["pick_order"]
    assert session.rounds == replay["rounds"]
    assert session.total_picks == len(replay["picks"])
    return session


def _mine(replay) -> list[int]:
    """The overall picks belonging to seat 3, from the capture itself."""
    team_id = replay["pick_order"][MY_SLOT - 1]
    return [p["overall"] for p in replay["picks"] if p["team"] == team_id]


async def _record(session_id, pick, by_espn, replay, **overrides):
    body = dict(
        espn_player_id=pick["espn_player_id"],
        by_me=pick["team"] == replay["pick_order"][MY_SLOT - 1],
        source="manual",
    )
    body.update(overrides)
    return await DraftService.add_pick(session_id, DraftPickCreate(**body))


async def test_the_whole_captured_draft_replays_pick_for_pick(team, board_players, replay):
    """All 52 picks, in order, posted the way the room posts them: the player
    only, letting the service assign the number. Every recorded row must match
    what ESPN actually did."""
    session = await _open_room(team, replay)
    order, size = replay["pick_order"], replay["league_size"]

    for pick in replay["picks"]:
        resp = await _record(session.id, pick, board_players, replay)
        got = resp.data
        assert got.overall_pick == pick["overall"], "the next pick number drifted from the draft"
        assert got.round == pick["round"]
        assert got.slot is not None and order[got.slot - 1] == pick["team"], (
            f"pick {pick['overall']} was credited to seat {got.slot}, ESPN gave it to team {pick['team']}"
        )
        assert got.player_id == board_players[pick["espn_player_id"]]

    detail = (await DraftService.get_session(session.id)).data
    assert detail.pick_count == len(replay["picks"])
    assert detail.next_overall_pick == len(replay["picks"]) + 1
    assert [p.overall_pick for p in detail.picks if p.by_me] == _mine(replay)
    # The draft is over, so no seat is on the clock — this replay is what
    # caught the unbounded search answering "pick 54" in a fourteenth round.
    assert detail.my_next_pick is None and detail.picks_until_my_turn is None


async def test_the_clock_counts_down_to_each_of_my_turns(team, board_players, replay):
    """Whose-turn arithmetic, checked at every pick rather than at the end:
    `picks_until_my_turn` must reach 0 exactly on my picks and never skip."""
    session = await _open_room(team, replay)
    mine = set(_mine(replay))

    for pick in replay["picks"]:
        detail = (await DraftService.get_session(session.id)).data
        expected = min((n for n in mine if n >= pick["overall"]), default=None)
        assert detail.my_next_pick == expected, f"before pick {pick['overall']}"
        if expected is not None:
            assert detail.picks_until_my_turn == expected - pick["overall"]
        await _record(session.id, pick, board_players, replay)


async def test_an_undo_mid_draft_refills_the_hole_without_moving_the_draft(team, board_players, replay):
    """The correction workflow: undo pick 20 of 30, and the room offers 20 as
    the next number while still standing at pick 31."""
    session = await _open_room(team, replay)
    for pick in replay["picks"][:30]:
        await _record(session.id, pick, board_players, replay)

    undone = replay["picks"][19]           # overall 20
    await DraftService.remove_pick(session.id, undone["overall"])

    detail = (await DraftService.get_session(session.id)).data
    assert detail.next_overall_pick == 20          # the hole a correction refills
    assert detail.pick_count == 29
    # The draft front is still just past pick 30, so my next turn is unchanged.
    front_turn = min(n for n in _mine(replay) if n >= 31)
    assert detail.my_next_pick == front_turn

    # The player is back on the board, and re-recording him lands in the hole.
    inputs = DraftBoardService._fetch_inputs(frozenset(), session.id)
    assert board_players[undone["espn_player_id"]] not in inputs.session_picked
    resp = await _record(session.id, undone, board_players, replay)
    assert resp.data.overall_pick == 20 and resp.data.round == undone["round"]

    # And nobody can be drafted twice, however the correction is entered.
    with pytest.raises(ConflictError) as exc:
        await _record(session.id, undone, board_players, replay)
    assert exc.value.error_code == "DRAFT_PLAYER_ALREADY_DRAFTED"


async def test_keepers_ride_the_replay_without_becoming_the_front(team, board_players, replay):
    """Layered on the capture, which had none: seat 3 keeps its round-two and
    round-four players. Recorded up front they leave the board, but the draft
    still starts at pick 1 and steps over them."""
    session = await _open_room(team, replay)
    mine = _mine(replay)
    kept = [replay["picks"][n - 1] for n in (mine[1], mine[3])]     # rounds 2 and 4

    updated = (await DraftService.update_session(session.id, DraftSessionUpdate(keepers=[
        {"espn_player_id": k["espn_player_id"], "name": f"Replay {k['overall']:02d}", "round": k["round"]}
        for k in kept
    ]))).data
    assert [k.overall_pick for k in updated.keepers] == [k["overall"] for k in kept]

    for k in kept:
        await _record(session.id, k, board_players, replay, source="keeper", overall_pick=k["overall"])

    detail = (await DraftService.get_session(session.id)).data
    assert detail.pick_count == 2
    assert detail.next_overall_pick == 1              # nothing has been drafted on the clock
    assert detail.my_next_pick == mine[0]             # my first real turn, not the kept one

    # Replay up to the first kept pick: the front steps over it rather than stopping.
    for pick in replay["picks"][: kept[0]["overall"] - 1]:
        await _record(session.id, pick, board_players, replay)
    detail = (await DraftService.get_session(session.id)).data
    assert detail.next_overall_pick == kept[0]["overall"] + 1
    assert detail.my_next_pick == mine[2]             # round three, not the kept round-two pick

    # Both kept players are off the board and on my roster.
    inputs = DraftBoardService._fetch_inputs(frozenset(), session.id)
    for k in kept:
        assert board_players[k["espn_player_id"]] in inputs.session_mine


async def test_the_board_and_roster_track_the_record(team, league, board_players, replay):
    """The board is derived, so it has to agree with the record at every point:
    drafted players gone, mine placed with the positions caps count on."""
    session = await _open_room(team, replay)
    scoring = resolve_scoring(league)
    room = BoardSession(session_id=session.id, my_slot=MY_SLOT, rounds=replay["rounds"],
                        league_size=replay["league_size"], draft_type="snake")

    opening = await DraftBoardService.get_board(scoring, session=room)
    assert len(opening.data) == len(replay["picks"])        # nobody drafted yet
    assert opening.roster == []
    # Value descends with draft order, so the big board opens in that order.
    assert [r.player_id for r in opening.data[:3]] == [900_001, 900_002, 900_003]

    for pick in replay["picks"][:24]:
        await _record(session.id, pick, board_players, replay)

    board = await DraftBoardService.get_board(scoring, session=room)
    drafted = {board_players[p["espn_player_id"]] for p in replay["picks"][:24]}
    on_board = {r.player_id for r in board.data}
    assert on_board.isdisjoint(drafted), "a drafted player is still on the board"
    assert len(board.data) == len(replay["picks"]) - 24

    # My six picks so far are the roster, each carrying what the zone places on.
    mine_so_far = [n for n in _mine(replay) if n <= 24]
    assert {e.player_id for e in board.roster} == {
        board_players[replay["picks"][n - 1]["espn_player_id"]] for n in mine_so_far
    }
    assert all(e.primary_position in POSITIONS for e in board.roster)
    assert all(e.positions for e in board.roster)

    # cv_rank is the pre-draft big board: stable as picks remove rows.
    assert [r.cv_rank for r in board.data] == sorted(r.cv_rank for r in board.data)
    # Recommendations never offer someone already gone, or a capped player.
    assert {r.player_id for r in board.recommendations}.isdisjoint(drafted)
    assert not any(r.cap_blocked for r in board.data if r.player_id in
                   {rec.player_id for rec in board.recommendations})


async def test_the_centre_cap_closes_as_the_replay_fills_it(team, league, board_players, replay):
    """The league caps centres at 3. Drafting a fourth is refused by the board
    (flagged, never hidden) once the cap is spent."""
    session = await _open_room(team, replay)
    scoring = resolve_scoring(league)
    room = BoardSession(session_id=session.id, my_slot=MY_SLOT, league_size=replay["league_size"])

    centres = [p for i, p in enumerate(replay["picks"]) if POSITIONS[i % len(POSITIONS)] == "C"]
    for centre in centres[:3]:
        await _record(session.id, centre, board_players, replay, by_me=True)

    board = await DraftBoardService.get_board(scoring, session=room)
    remaining = [r for r in board.data if r.primary_position == "C"]
    assert remaining, "the capture should leave centres on the board"
    assert all(r.cap_blocked for r in remaining), "a fourth centre must be cap-blocked"
    assert not any(r.primary_position == "C" for r in board.recommendations)
    # Everyone else is still draftable.
    assert not any(r.cap_blocked for r in board.data if r.primary_position != "C")


async def test_a_room_opened_without_a_slot_comes_alive_when_one_is_set(team, board_players, replay):
    """The room can be created without a seat — the field is skippable, and an
    unsynced league has no pick order to choose one from. Everything that
    depends on the slot stays quiet until it is set, and starts working the
    moment it is, mid-draft and all. This is what the room's slot editor drives.
    """
    session = (await DraftService.create_session(
        team.user_id, DraftSessionCreate(team_id=team.team_id)
    )).data
    assert session.my_slot is None
    # The draft still runs; it just cannot say which of the seats is yours.
    assert session.my_next_pick is None and session.picks_until_my_turn is None

    for pick in replay["picks"][:8]:
        await _record(session.id, pick, board_players, replay)
    quiet = (await DraftService.get_session(session.id)).data
    assert quiet.pick_count == 8 and quiet.my_next_pick is None

    live = (await DraftService.update_session(
        session.id, DraftSessionUpdate(my_slot=MY_SLOT)
    )).data
    assert live.my_slot == MY_SLOT
    # From the front (pick 9), seat 3's next turn in the captured order.
    assert live.my_next_pick == min(n for n in _mine(replay) if n >= 9)
    assert live.picks_until_my_turn == live.my_next_pick - 9

    # ...and a seat the pick order does not have is still refused.
    with pytest.raises(BadRequestError) as exc:
        await DraftService.update_session(
            session.id, DraftSessionUpdate(my_slot=replay["league_size"] + 1)
        )
    assert exc.value.error_code == "DRAFT_SLOT_OUT_OF_RANGE"


async def test_the_board_paces_against_the_seats_the_real_picks_landed_in(
    team, league, board_players, replay
):
    """B5.1 end to end: the other seats' picks stop being anonymous.

    Every pick the room records carries the seat that made it, so a category
    board can read the three opposing rosters of this four-team draft and pace
    against what they actually hold. Nothing here fabricates a seat — they come
    out of the captured draft, through `add_pick`, into `usr.draft_picks.slot`,
    and back through the board's own fetch.

    The captured players are deliberately uniform in every category but points
    (see `board_players`), so what this pins is the wiring and the standings;
    the arithmetic on a differentiated pool is unit-tested.
    """
    league.scoring_type = "categories"
    league.category_win_mode = "each_category"
    league.categories = [
        {"key": k, "label": k.upper(), "higher_is_better": k != "tov", "is_rate": False}
        for k in ("pts", "reb", "ast", "stl", "blk", "tov")
    ]
    league.save()

    session = await _open_room(team, replay)
    scoring = resolve_scoring(league)
    assert scoring.is_categories
    room = BoardSession(session_id=session.id, my_slot=MY_SLOT, rounds=replay["rounds"],
                        league_size=replay["league_size"], draft_type="snake")

    # Before anybody has drafted there is nothing to pace against.
    opening = await DraftBoardService.get_board(scoring, session=room)
    assert opening.meta.pace_source == "tier" and opening.meta.seats_counted == 0

    # One full round: every seat now holds exactly one player.
    for pick in replay["picks"][:replay["league_size"]]:
        await _record(session.id, pick, board_players, replay)

    board = await DraftBoardService.get_board(scoring, session=room)

    # Three opponents in a four-team draft — the minimum worth reading, and
    # every one of them came from a real pick's seat.
    assert board.meta.pace_source == "seats"
    assert board.meta.seats_counted == replay["league_size"] - 1
    seats_recorded = {
        p.slot for p in DraftPick.select().where(DraftPick.session == session.id)
    }
    assert seats_recorded == set(range(1, replay["league_size"] + 1))

    need = {n.key: n for n in board.meta.category_need}
    assert set(need) == {"pts", "reb", "ast", "stl", "blk", "tov"}
    for entry in need.values():
        assert entry.seats == replay["league_size"]         # the room, me included
        assert 1 <= entry.my_rank <= replay["league_size"]
    # Value descends with draft order and seat 3 picked third, so on points —
    # the one category these players differ in — two teams are ahead of me.
    assert need["pts"].my_rank == 3

    # And the fit column is live: every scorable row carries one.
    assert all(r.fit_value is not None for r in board.data if r.value is not None)
