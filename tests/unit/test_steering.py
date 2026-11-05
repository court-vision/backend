"""
services/steering.py: Dime's holds as inputs to the planners (the Dime autopilot plan, step 4),
and how the planners take them, without Postgres or ESPN.

Covers the rules (`steer_fill`, `lane_steer`, `blocking`, `share_on`), the words, the read
(off by default, and never raising), and the plans that read them: auto-lineup's fill (a pin
holds a player where he is, an override at 0 treats him as out), the multi-spot stream planner
(a keep holds a spot's player in it, a block keeps a free agent out) and `plan_lanes` (an
override scales a player's value on its days, roster and pool alike).
"""

import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from core.settings import settings
from services import steering
from services.lineup_planner import PlannerPlayer, plan_fill
from services.multi_stream_planner import Streamer, plan_streams
from services.steering import BLOCK, KEEP, NO_STEER, OVERRIDE, PIN, Hold

PG, SG, SF, PF, C, G, F, UT, BE, IR = 0, 1, 2, 3, 4, 5, 6, 11, 12, 13
D0 = date(2026, 11, 16)
D1, D2, D3 = date(2026, 11, 17), date(2026, 11, 18), date(2026, 11, 19)


def hold(kind, pid, start=D0, end=None, *, share=None, hid=1, reason="why"):
    return Hold(id=hid, kind=kind, player_id=pid, starts_on=start, ends_on=end or start, share=share, reason=reason,
                name=f"Player {pid}")


def pp(pid, slot, eligible=(PG, UT, BE), *, game=True, status=None, locked=False, value=10.0):
    return PlannerPlayer(player_id=pid, name=f"P{pid}", slot_id=slot, eligible_slot_ids=frozenset(eligible),
                         has_game=game, injury_status=status, locked=locked, value=value)


@pytest.fixture
def steering_on(monkeypatch):
    monkeypatch.setattr(settings, "dime_steering_enabled", True)


# ---- the rules ------------------------------------------------------------------------------


@pytest.mark.unit
def test_a_hold_covers_its_days_and_no_others():
    h = hold(KEEP, 1, D1, D2)
    assert [h.covers(d) for d in (D0, D1, D2, D3)] == [False, True, True, False]
    assert steering.on_day([h, hold(PIN, 2, D0)], D1) == [h]
    assert steering.on_day([h, hold(PIN, 2, D1)], D1, PIN)[0].player_id == 2


@pytest.mark.unit
def test_a_players_share_is_his_overrides_or_one_and_the_later_override_counts():
    holds = [hold(OVERRIDE, 1, D0, D2, share=0.5, hid=1), hold(OVERRIDE, 1, D1, share=0.0, hid=2),
             hold(KEEP, 1, D0, D3, hid=3)]
    assert [steering.share_on(holds, 1, d) for d in (D0, D1, D2, D3)] == [0.5, 0.0, 0.5, 1.0]
    assert steering.share_on(holds, 2, D0) == 1.0
    # The live list is by start day: a newer override that starts earlier still wins its days.
    by_start = [hold(OVERRIDE, 1, D0, D2, share=0.5, hid=8), hold(OVERRIDE, 1, D1, D2, share=0.0, hid=7)]
    assert [steering.share_on(by_start, 1, d) for d in (D0, D1, D2)] == [0.5, 0.5, 0.5]


@pytest.mark.unit
def test_the_fill_sees_a_pinned_player_locked_and_one_counted_out_as_out():
    board = [pp(1, BE), pp(2, PG), pp(3, BE, game=False)]
    holds = [hold(PIN, 1, D0), hold(OVERRIDE, 2, D0, share=0.0), hold(OVERRIDE, 3, D0, share=0.0)]
    seen = {p.player_id: p for p in steering.steer_fill(board, holds, D0)}
    assert seen[1].locked and not seen[2].locked
    assert seen[2].injury_status == "OUT"
    assert seen[3].injury_status is None          # no game: nothing to be out of
    # Off its days, and at a share above 0, the board is as ESPN has it
    assert steering.steer_fill(board, holds, D1) == board
    assert steering.steer_fill(board, [hold(OVERRIDE, 2, D0, share=0.5)], D0) == board


@pytest.mark.unit
def test_a_pin_keeps_the_fill_from_starting_a_benched_player_and_an_out_override_lets_the_bench_in():
    counts = {PG: 1, BE: 2}
    board = [pp(1, BE, value=30.0), pp(2, PG, value=10.0, game=False)]
    # Unsteered, the fill starts the bench player with a game in the starter's slot (he has none)
    assert {(m.player_id, m.to_slot_id) for m in plan_fill(board, counts).moves} == {(1, PG), (2, BE)}
    # Pinned on the bench, he stays there, and nobody moves through his slot
    assert steering.fill_with(board, counts, [hold(PIN, 1, D0)], D0).is_noop
    # The starter counted out (Dime expects him to miss): the bench player with a game takes his slot
    board = [pp(1, BE, value=5.0), pp(2, PG, value=10.0)]
    assert plan_fill(board, counts).is_noop
    moves = steering.fill_with(board, counts, [hold(OVERRIDE, 2, D0, share=0.0)], D0).moves
    assert {(m.player_id, m.to_slot_id) for m in moves} == {(1, PG), (2, BE)}
    # No day or no holds: the plain fill
    assert steering.fill_with(board, counts, [hold(OVERRIDE, 2, D0, share=0.0)], None).is_noop


@pytest.mark.unit
def test_the_lanes_see_shares_by_day_blocks_and_keeps_by_day_index_and_no_pins():
    days = [D0, D1, D2]
    holds = [hold(OVERRIDE, 1, D1, share=0.5), hold(OVERRIDE, 4, D3, share=0.0), hold(BLOCK, 2, D0, D1),
             hold(KEEP, 3, D2, D3), hold(PIN, 5, D0)]
    s = steering.lane_steer(holds, days)
    assert s.shares == {1: (1.0, 0.5, 1.0)}           # 4's override falls outside the horizon
    assert s.no_add == {2: frozenset({0, 1})} and s.kept == {3: frozenset({2})}
    assert s.share(1, 1) == 0.5 and s.share(9, 0) == 1.0 and not s.empty
    assert steering.lane_steer([], days) is NO_STEER and NO_STEER.empty
    assert steering.lane_steer([hold(PIN, 5, D0)], days).empty


@pytest.mark.unit
def test_a_proposed_move_runs_into_a_block_on_its_add_or_a_keep_on_its_drop():
    holds = [hold(BLOCK, 10, D0, D1, hid=1), hold(KEEP, 20, D1, hid=2)]
    assert steering.blocking(holds, add=10, drop=None, day=D0).id == 1
    assert steering.blocking(holds, add=None, drop=20, day=D1).id == 2
    assert steering.blocking(holds, add=10, drop=20, day=D2) is None
    assert steering.blocking(holds, add=20, drop=10, day=D1) is None    # a keep doesn't stop his add


@pytest.mark.unit
def test_the_words():
    assert steering.day_span(D0, D0) == "Mon 11/16"
    assert steering.day_span(D0, D2) == "Mon 11/16 to Wed 11/18"
    assert steering.kind_words(OVERRIDE, 0) == "expected to miss"
    assert steering.kind_words(OVERRIDE, 0.5) == "counted at 50% of his value"
    assert steering.kind_words(PIN) == "pinned in his slot"


# ---- the read -------------------------------------------------------------------------------


def _row(**kw):
    base = dict(id=7, kind=KEEP, player_id=1, starts_on=D0, ends_on=D1, share=None, reason="r", player_name="A")
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.mark.unit
def test_switched_off_no_hold_is_read_at_all(monkeypatch):
    async def never(team_id, today=None):
        raise AssertionError("read while off")
    monkeypatch.setattr(steering, "live_rows", never)
    assert asyncio.run(steering.holds_for(21, D0)) == []


@pytest.mark.unit
def test_switched_on_the_live_holds_are_read_and_a_failed_read_is_no_holds(monkeypatch, steering_on):
    async def rows(team_id, today=None):
        return [_row(), _row(id=8, kind=OVERRIDE, share=0.5)]
    monkeypatch.setattr(steering, "live_rows", rows)
    got = asyncio.run(steering.holds_for(21, D0))
    assert [(h.id, h.kind, h.share) for h in got] == [(7, KEEP, None), (8, OVERRIDE, 0.5)]

    async def broken(team_id, today=None):
        raise RuntimeError("db down")
    monkeypatch.setattr(steering, "live_rows", broken)
    assert asyncio.run(steering.holds_for(21, D0)) == []


@pytest.mark.unit
def test_a_sent_lineup_chip_pins_only_the_players_it_benched(monkeypatch, steering_on):
    made = []

    class FakeHold:
        @staticmethod
        def create(**kw):
            made.append(kw)

    import db.models.team_holds as model
    monkeypatch.setattr(model, "TeamHold", FakeHold)
    row = SimpleNamespace(id=31, team_id=21, user_id=11, nba_date=D0, reason="Rest night", moves=[
        {"espn_id": 1, "name": "A", "role": "bench", "team": "DEN"},
        {"espn_id": 2, "name": "B", "role": "start"},
        {"espn_id": 3, "name": "C", "role": "shift"},
    ])
    assert steering._pin_benched.__wrapped__(row, None) == 1       # the repository function, without run_db
    assert [(m["kind"], m["player_id"], m["starts_on"], m["ends_on"], m["proposal_id"], m["reason"])
            for m in made] == [(PIN, 1, D0, D0, 31, "Rest night")]


@pytest.mark.unit
def test_switched_off_a_sent_lineup_chip_pins_nobody(monkeypatch):
    async def never(*a, **kw):
        raise AssertionError("read while off")
    monkeypatch.setattr(steering, "run_db", never)
    asyncio.run(steering.pin_benched(31))


# ---- the planners ---------------------------------------------------------------------------


def s(pid, *values, eligible=(UT,), available_from=0, kept=(), no_add=()):
    return Streamer(pid, tuple(float(v) for v in values), frozenset(eligible), available_from, None,
                    no_add=frozenset(no_add), kept=frozenset(kept))


def empty(horizon):
    return [[] for _ in range(horizon)]


@pytest.mark.unit
def test_a_kept_holder_stays_in_his_spot_on_his_days():
    candidates = [s(3, 30, 30, 30)]
    free = plan_streams([s(1, 5, 5, 5)], candidates, empty(3), {UT: 1}, adds=None)
    assert free.spots == ((3, 3, 3),)
    kept = plan_streams([s(1, 5, 5, 5, kept=(0, 1))], candidates, empty(3), {UT: 1}, adds=None)
    assert kept.spots == ((1, 1, 3),) and kept.value == 5 + 5 + 30


@pytest.mark.unit
def test_a_blocked_free_agent_isnt_added_on_his_days():
    holders = [s(1, 5, 5, 5)]
    plan = plan_streams(holders, [s(3, 30, 30, 30, no_add=(0,))], empty(3), {UT: 1}, adds=None)
    assert plan.spots == ((1, 3, 3),)
    plan = plan_streams(holders, [s(3, 30, 30, 30, no_add=(0, 1, 2)), s(4, 20, 20, 20)], empty(3), {UT: 1},
                        adds=None)
    assert plan.spots == ((4, 4, 4),)


@pytest.mark.unit
def test_the_lanes_plan_with_the_holds():
    from tests.unit.test_stream_lane_rules import DAYS, HOLDER, ROSTER, board, fa
    from services.stream_lane_service import Budget, plan_lanes

    def plan(pool, steer=NO_STEER):
        return plan_lanes(board(ROSTER), [HOLDER], pool, DAYS, [], Budget(None, (None, None, None)), 2, None, steer)

    pool = [fa(10, "LAL"), fa(11, "MIA", value=40.0)]
    assert 11 in plan(pool).spots[0]
    days = [d.nba_date for d in DAYS]
    # Blocked every day: the next best free agent takes his place
    blocked = plan(pool, steering.lane_steer([hold(BLOCK, 11, days[0], days[2])], days))
    assert 11 not in blocked.spots[0]
    # Counted out on his game days: worth nothing to the spot, so he isn't taken
    out = plan(pool, steering.lane_steer([hold(OVERRIDE, 11, days[0], days[2], share=0.0)], days))
    assert 11 not in out.spots[0]
    # The holder kept: he holds the spot through the keep's last day
    kept = plan(pool, steering.lane_steer([hold(KEEP, HOLDER, days[0], days[1])], days))
    assert kept.spots[0][:2] == (HOLDER, HOLDER)
    # Doubled: a holder worth twice his value outplays a 22-point free agent he otherwise loses to
    doubled = plan([fa(10, "LAL"), fa(12, "MIA")], steering.lane_steer(
        [hold(OVERRIDE, HOLDER, days[0], days[2], share=2.0)], days))
    assert doubled.spots[0][0] == HOLDER
