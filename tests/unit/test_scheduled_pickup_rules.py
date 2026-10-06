"""
The pure rules behind scheduled pickups: when the first attempt is (`attempt_window`),
what a pre-send refusal means for a stored row (`on_refusal`), and which seat the new
player gets on day D (`pick_seat`). No I/O — the schedule lookups are injected.
"""

from datetime import date, datetime, time, timezone

import pytest

from schemas.common import FantasyProvider
from schemas.lineup_editor import LineupPlayer, LineupState
from services import scheduled_pickup_service as svc

PG, SG, UT, BE, IR = 0, 1, 11, 12, 13
D = date(2026, 11, 12)          # a Thursday in EST
PREV = date(2026, 11, 11)


def utc(y, m, d, hh, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def window(drop_team=None, *, tips=None, playing=None):
    tips = tips or {}
    playing = playing or {}
    return svc.attempt_window(D, drop_team, first_tip=lambda d: tips.get(d), teams_playing=lambda d: playing.get(d, set()))


# ---- attempt_window -----------------------------------------------------------------


@pytest.mark.unit
def test_first_attempt_is_the_previous_days_first_tip():
    w = window(tips={PREV: time(19, 0), D: time(19, 30)})
    assert w.rule == "first_tip_prev"
    assert w.not_before_at == utc(2026, 11, 12, 0, 0)      # 7 PM EST Wed = 00:00 UTC Thu
    assert w.deadline_at == utc(2026, 11, 13, 0, 30)       # 7:30 PM EST Thu


@pytest.mark.unit
def test_a_drop_who_plays_the_day_before_waits_for_the_rollover():
    w = window("MIN", tips={PREV: time(19, 0), D: time(19, 30)}, playing={PREV: {"MIN", "LAL"}})
    assert w.rule == "rollover_into_day"
    assert w.not_before_at == utc(2026, 11, 12, 7, 0)      # 2 AM EST Thu
    assert w.deadline_at == utc(2026, 11, 13, 0, 30)


@pytest.mark.unit
def test_the_drop_teams_abbreviation_is_normalised_before_the_lookup():
    w = window("PHO", tips={PREV: time(19, 0)}, playing={PREV: {"PHX"}})
    assert w.rule == "rollover_into_day"


@pytest.mark.unit
def test_a_drop_who_does_not_play_keeps_the_first_tip_rule():
    w = window("MIN", tips={PREV: time(19, 0)}, playing={PREV: {"LAL", "BOS"}})
    assert w.rule == "first_tip_prev" and w.not_before_at == utc(2026, 11, 12, 0, 0)


@pytest.mark.unit
def test_no_games_the_day_before_means_the_rollover_into_that_day():
    w = window(tips={D: time(19, 30)})
    assert w.rule == "rollover_into_prev"
    assert w.not_before_at == utc(2026, 11, 11, 7, 0)      # 2 AM EST Wed


@pytest.mark.unit
def test_no_schedule_for_day_d_means_no_deadline():
    w = window(tips={PREV: time(19, 0)})
    assert w.deadline_at is None


@pytest.mark.unit
def test_rollover_crosses_both_dst_switches_without_raising():
    # 2026-11-01 (fall back) and 2027-03-14 (spring forward) are the switch days.
    fall = svc.rollover_at(date(2026, 11, 1))
    spring = svc.rollover_at(date(2027, 3, 14))
    assert fall.tzinfo is timezone.utc and spring.tzinfo is timezone.utc
    assert fall == utc(2026, 11, 1, 7, 0)                  # 2 AM EST (the clocks just fell back)
    assert abs((spring - utc(2027, 3, 14, 7, 0)).total_seconds()) <= 3600


@pytest.mark.unit
def test_et_at_during_daylight_time():
    assert svc.et_at(date(2026, 10, 21), time(19, 30)) == utc(2026, 10, 21, 23, 30)


# ---- on_refusal ---------------------------------------------------------------------


def refusal(reason, *, period=2, day=3, seat_free=False):
    return svc.on_refusal(reason, period=period, day=day, nba_date=date(2026, 10, 22), seat_free=seat_free)


@pytest.mark.unit
@pytest.mark.parametrize("reason", ["add_locked", "drop_locked"])
def test_a_lock_the_day_before_waits_for_the_rollover(reason):
    d = refusal(reason)
    assert d.action == "defer" and d.at == svc.rollover_at(date(2026, 10, 22)) and d.after is None


@pytest.mark.unit
def test_a_locked_add_on_the_day_itself_has_expired():
    d = refusal("add_locked", period=3)
    assert (d.action, d.status, d.reason) == ("settle", "expired", "locked_on_day")


@pytest.mark.unit
def test_a_locked_drop_on_the_day_becomes_add_only_when_a_seat_is_free():
    assert refusal("drop_locked", period=3, seat_free=True) == svc.Decision("add_only", reason="drop_locked")
    assert refusal("drop_locked", period=3, seat_free=False) == svc.Decision("settle", "skipped", "drop_locked")


@pytest.mark.unit
def test_waivers_retry_hourly():
    d = refusal("add_on_waivers")
    assert d.action == "defer" and d.after == svc.WAIVER_RETRY


@pytest.mark.unit
@pytest.mark.parametrize("reason", ["add_not_found", "add_not_available"])
def test_a_player_who_is_gone_is_the_no_op(reason):
    assert refusal(reason) == svc.Decision("settle", "skipped", "unavailable")


@pytest.mark.unit
def test_already_on_roster_is_skipped():
    assert refusal("add_already_on_roster") == svc.Decision("settle", "skipped", "already_on_roster")


@pytest.mark.unit
def test_a_missing_drop_becomes_add_only_when_a_seat_is_free():
    assert refusal("drop_not_on_roster", seat_free=True) == svc.Decision("add_only", reason="drop_missing")
    assert refusal("drop_not_on_roster") == svc.Decision("settle", "skipped", "drop_missing")


@pytest.mark.unit
def test_an_unknown_reason_fails_the_row():
    assert refusal("same_player") == svc.Decision("settle", "failed", "same_player")


# ---- pick_seat ----------------------------------------------------------------------


def player(pid, slot, *, eligible=(PG, UT, BE)):
    return LineupPlayer(player_id=pid, name=str(pid), team="DEN", lineup_slot_id=slot, lineup_slot=str(slot),
                        eligible_slot_ids=list(eligible), eligible_slots=[str(s) for s in eligible])


def state(players, counts=None):
    return LineupState(provider=FantasyProvider.ESPN, team_name="T", espn_team_id=1, nba_date="2026-10-22",
                       scoring_period_id=3, scoring_period_source="provider",
                       slot_counts=counts or {"0": 1, "1": 1, "11": 1, "12": 2, "13": 1},
                       players=players, roster_version="v", fetched_at="now")


@pytest.mark.unit
def test_the_vacated_seat_is_preferred_when_open_and_eligible():
    me = player(9, BE)
    board = state([me, player(1, PG)])              # UT (the drop's seat) is empty, PG is taken
    assert svc.pick_seat(board, me, preferred=UT) == UT


@pytest.mark.unit
def test_an_ineligible_preferred_seat_falls_back_to_the_first_open_eligible_one():
    me = player(9, BE, eligible=(PG, UT, BE))
    board = state([me, player(1, UT)])              # SG is open but he cannot play it; PG is open
    assert svc.pick_seat(board, me, preferred=SG) == PG


@pytest.mark.unit
def test_lowest_slot_first_keeps_ut_free():
    me = player(9, BE)
    board = state([me])
    assert svc.pick_seat(board, me, preferred=None) == PG


@pytest.mark.unit
def test_no_open_eligible_slot_means_the_bench():
    me = player(9, BE)
    board = state([me, player(1, PG), player(2, UT)])
    assert svc.pick_seat(board, me, preferred=UT) is None


@pytest.mark.unit
def test_his_own_current_slot_does_not_count_as_taken():
    me = player(9, UT)                               # already seated (ESPN did it); UT counts as open for him
    board = state([me, player(1, PG)])
    assert svc.pick_seat(board, me, preferred=UT) == UT
