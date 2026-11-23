"""
services.team_overview_service without ESPN or Postgres.

The rules are pure and tested as such: what a day's action needs from the user against
the team's automations (`judge`), the team's one state, tonight's seat marks, the week
strip, the season line and what is scheduled next. The reads behind `overview` are
fakes: a board per team (or its failure), a matchup, the plan rows and the league's
projection, so the tests can say what one team's failure does to the others, what is
kept and for how long, and that a league projection is never waited on for long.
"""

import asyncio
from datetime import date, time, timedelta, timezone
from types import SimpleNamespace

import pytest

from core import inflight
from core.errors import ProviderAuthError
from schemas.common import ApiStatus, FantasyProvider, LeagueInfo
from schemas.daily_actions import DailyAction, DailyActionPlayer, DailyActionsData, DailyActionsResp
from schemas.lineup_editor import LineupMoveResult, LineupPlayer, LineupSlotDef, LineupState
from services import team_overview_service as svc
from services.ai import autonomy
from services.league_projection_service import LeagueProjectionUnavailable
from services.scheduled_pickup_service import et_at

pytestmark = pytest.mark.unit

PG, SG, C, UT, BE, IR = 0, 1, 4, 11, 12, 13
NAMES = {PG: "PG", SG: "SG", C: "C", UT: "UT", BE: "BE", IR: "IR"}
COUNTS = {"0": 1, "1": 1, "4": 1, "11": 2, "12": 2, "13": 1}        # five starting seats
SLOTS = [LineupSlotDef(slot_id=s, slot=NAMES[s], count=n)
         for s, n in ((PG, 1), (SG, 1), (C, 1), (UT, 2), (BE, 2), (IR, 1))]
DAY = date(2026, 11, 19)                                             # a Thursday


def et(hh, mm=0, d=DAY):
    return et_at(d, time(hh, mm))


NOW = et(17, 0)
ESPN = LeagueInfo(provider=FantasyProvider.ESPN, league_id=1, team_name="Goblins", year=2027,
                  espn_s2="s2", swid="{SWID}", espn_team_id=1)


def player(pid, name, slot, *, game=True, tip="19:30", status=None, started=False, team="DEN"):
    return LineupPlayer(
        player_id=pid, nba_player_id=pid * 10, name=name, team=team, lineup_slot_id=slot, lineup_slot=NAMES[slot],
        eligible_slot_ids=[PG, SG, UT, BE, IR], eligible_slots=["PG", "SG", "UT", "BE", "IR"],
        injured=status == "OUT", injury_status=status, has_game_today=game, opponent="vs LAL" if game else None,
        game_time_et=tip if game else None, game_started=started, locked=started,
        playable=game and status != "OUT", avg_points=30.0,
    )


def board(players, *, nba_date=DAY.isoformat(), can_write=True):
    return LineupState(
        provider=FantasyProvider.ESPN, team_name="Goblins", espn_team_id=1, nba_date=nba_date, scoring_period_id=31,
        first_game_time_et="19:00", slot_counts=COUNTS, slots=SLOTS, players=players, can_write=can_write,
        roster_version="v1", fetched_at="now",
    )


def move(pid, frm, to, role="start"):
    return LineupMoveResult(player_id=pid, name=f"P{pid}", from_slot_id=frm, from_slot=NAMES[frm],
                            to_slot_id=to, to_slot=NAMES[to], role=role)


def action(kind, pid, *, moves=(), counterpart=None, tip=None, blocked=None):
    other = DailyActionPlayer(player_id=counterpart, nba_player_id=counterpart * 10, name=f"P{counterpart}",
                              team="DEN") if counterpart else None
    return DailyAction(id=f"{kind}:{pid}", kind=kind, title=f"{kind} {pid}",
                       player=DailyActionPlayer(player_id=pid, nba_player_id=pid * 10, name=f"P{pid}", team="DEN"),
                       counterpart=other, moves=list(moves), blocked_reason=blocked, game_time_et=tip)


START = action("start", 11, moves=[move(11, BE, UT), move(8, UT, BE, "bench")], counterpart=8, tip="19:30")
SCRATCH = action("scratch", 11, moves=[move(11, BE, UT), move(8, UT, BE, "bench")], counterpart=8, tip="19:30")
STREAM = action("add_drop", 900, counterpart=12, tip="20:00")
TO_IR = action("ir_in", 8, moves=[move(8, UT, IR, "shift")])
TIP = et(19, 30)


# ------------------------------- deadlines ------------------------------- #


def test_a_lineup_move_is_due_at_the_earliest_tip_among_the_players_it_moves():
    state = board([player(8, "Out", UT, tip="19:00", status="OUT"), player(11, "Filler", BE, tip="21:30")])
    assert svc.deadline_of(SCRATCH, state) == "19:00"            # his seat locks when his own game tips
    assert svc.deadline_of(STREAM, state) is None and svc.deadline_of(TO_IR, state) is None


def test_a_move_whose_bench_side_has_no_game_is_due_at_the_fillers_tip():
    state = board([player(8, "Idle", UT, game=False), player(11, "Filler", BE, tip="21:30")])
    assert svc.deadline_of(START, state) == "21:30"
    assert svc.tip_at(DAY.isoformat(), "21:30") == et(21, 30)
    assert svc.tip_at(None, "21:30") is None and svc.tip_at(DAY.isoformat(), "soon") is None


# ------------------------------- what an item needs ------------------------------- #

WRITABLE = svc.Rules(can_write=True)
FILL = svc.Rules(can_write=True, fill_applies=True, fill_closes_at=et(18, 45))


def test_a_lineup_move_nothing_will_make_needs_the_user():
    assert svc.judge(START, TIP, WRITABLE, NOW) == ("needs_you", None)
    assert svc.judge(SCRATCH, TIP, WRITABLE, NOW) == ("needs_you", None)


def test_auto_lineup_covers_a_lineup_move_until_its_window_closes():
    assert svc.judge(START, TIP, FILL, NOW) == ("covered", "auto_lineup")
    assert svc.judge(SCRATCH, TIP, FILL, NOW) == ("covered", "auto_lineup")
    assert svc.judge(START, TIP, FILL, et(18, 45)) == ("needs_you", None)


@pytest.mark.parametrize("change", [{"fill_ran": True}, {"fill_failed": True}, {"can_write": False},
                                    {"fill_closes_at": None}])
def test_auto_lineup_is_not_counted_on_once_it_ran_failed_or_cannot_write(change):
    rules = svc.Rules(**{**FILL.__dict__, **change})
    assert svc.judge(START, TIP, rules, NOW) == ("needs_you", None)


JUST_RULED = {80: NOW - timedelta(minutes=1)}      # our verdict on the benched starter changed a minute ago


def test_a_late_scratch_is_covered_at_act_and_needs_the_user_at_notify():
    ran = {"can_write": True, "fill_applies": True, "fill_ran": True, "fill_closes_at": et(18, 45),
           "ruled_at": JUST_RULED}
    act = svc.Rules(**ran, late_scratch=autonomy.ACT)
    assert svc.judge(SCRATCH, TIP, act, NOW) == ("covered", "late_scratch")
    assert svc.judge(SCRATCH, TIP, svc.Rules(**ran, late_scratch=autonomy.NOTIFY), NOW) == ("needs_you", None)
    assert svc.judge(START, TIP, act, NOW) == ("needs_you", None)       # the swap answers a ruling, not an open seat


def test_the_swap_answers_a_ruling_once_so_a_scratch_it_left_on_the_board_needs_the_user():
    act = dict(can_write=True, late_scratch=autonomy.ACT)
    on_its_way = svc.Rules(**act, ruled_at={80: NOW - svc.SWAP_ANSWERS_WITHIN + timedelta(seconds=1)})
    assert svc.judge(SCRATCH, TIP, on_its_way, NOW) == ("covered", "late_scratch")
    # the change was posted and the scratch is still here: it found no swap, skipped him or was never posted
    left = svc.Rules(**act, ruled_at={80: NOW - svc.SWAP_ANSWERS_WITHIN})
    assert svc.judge(SCRATCH, TIP, left, NOW) == ("needs_you", None)
    # out since his last game: no change of ours today, so nothing was posted (or the verdicts weren't read)
    assert svc.judge(SCRATCH, TIP, svc.Rules(**act), NOW) == ("needs_you", None)
    assert svc.judge(SCRATCH, TIP, svc.Rules(**act, ruled_at={999: NOW}), NOW) == ("needs_you", None)


@pytest.mark.parametrize("spent", [{"scratch_failed": frozenset({80})}, {"scratch_answered": frozenset({80})},
                                   {"scratch_spent": True}])
def test_a_swap_that_failed_was_made_once_for_him_or_is_past_the_days_limit_needs_the_user(spent):
    rules = svc.Rules(can_write=True, late_scratch=autonomy.ACT, ruled_at=JUST_RULED, **spent)
    assert svc.judge(SCRATCH, TIP, rules, NOW) == ("needs_you", None)


def test_another_players_failed_or_answered_swap_is_not_this_ones():
    other = svc.Rules(can_write=True, late_scratch=autonomy.ACT, ruled_at=JUST_RULED,
                      scratch_failed=frozenset({999}), scratch_answered=frozenset({998}))
    assert svc.judge(SCRATCH, TIP, other, NOW) == ("covered", "late_scratch")


def test_ten_minutes_from_the_lock_no_automation_is_counted_on():
    rules = svc.Rules(can_write=True, late_scratch=autonomy.ACT, ruled_at={80: TIP - timedelta(minutes=12)})
    assert svc.judge(SCRATCH, TIP, rules, TIP - timedelta(minutes=11)) == ("covered", "late_scratch")
    assert svc.judge(SCRATCH, TIP, rules, TIP - timedelta(minutes=10)) == ("needs_you", None)


def test_dime_covers_a_ruling_while_its_event_check_in_answers_it():
    dime = frozenset({autonomy.LATE_SCRATCH})
    fresh = svc.Rules(can_write=True, dime_acts=dime, ruled_at={80: NOW - timedelta(minutes=24)})
    assert svc.judge(SCRATCH, TIP, fresh, NOW) == ("covered", "dime")
    stale = svc.Rules(can_write=True, dime_acts=dime, ruled_at={80: NOW - svc.DIME_ANSWERS_WITHIN})
    assert svc.judge(SCRATCH, TIP, stale, NOW) == ("needs_you", None)
    assert svc.judge(SCRATCH, TIP, svc.Rules(can_write=True, dime_acts=dime), NOW) == ("needs_you", None)
    # a check-in before the lock doesn't help where Dime doesn't set the lineup
    waiting = svc.Rules(can_write=True, dime_acts=dime, next_checkin_at=et(18, 15))
    assert svc.judge(SCRATCH, TIP, waiting, NOW) == ("needs_you", None)


def test_dime_covers_a_lineup_move_only_with_a_check_in_before_the_lock():
    dime = frozenset({autonomy.LINEUP, autonomy.LATE_SCRATCH})
    before = svc.Rules(can_write=True, dime_acts=dime, next_checkin_at=et(18, 15))
    assert svc.judge(START, TIP, before, NOW) == ("covered", "dime")
    assert svc.judge(SCRATCH, TIP, before, NOW) == ("covered", "dime")       # a ruling it didn't answer, too
    after = svc.Rules(can_write=True, dime_acts=dime, next_checkin_at=et(6, 0, DAY + timedelta(days=1)))
    assert svc.judge(START, TIP, after, NOW) == ("needs_you", None)
    assert svc.judge(SCRATCH, TIP, after, NOW) == ("needs_you", None)
    assert svc.judge(START, TIP, svc.Rules(can_write=True, dime_acts=dime), NOW) == ("needs_you", None)


def test_optional_moves_are_the_users_call_unless_an_automation_makes_them():
    assert svc.judge(STREAM, None, WRITABLE, NOW) == ("your_call", None)
    assert svc.judge(TO_IR, None, WRITABLE, NOW) == ("your_call", None)
    dime = svc.Rules(can_write=True, dime_acts=frozenset({autonomy.DAILY_ACTIONS}))
    assert svc.judge(STREAM, None, dime, NOW) == ("covered", "dime")
    act = svc.Rules(can_write=True, daily_actions=autonomy.ACT)
    assert svc.judge(TO_IR, None, act, NOW) == ("covered", "daily_actions")
    assert svc.judge(STREAM, None, act, NOW) == ("your_call", None)         # act never drops a player
    stuck = action("ir_out", 14, blocked="roster_full")             # a row nobody can send as it is
    assert svc.judge(stuck, None, act, NOW) == ("your_call", None)
    assert svc.judge(stuck, None, dime, NOW) == ("your_call", None)
    assert svc.judge(TO_IR, None, svc.Rules(can_write=False, daily_actions=autonomy.ACT), NOW) == ("your_call", None)


def test_items_come_with_what_needs_the_user_first_and_the_soonest_lock_first():
    late = action("start", 12, moves=[move(12, BE, SG)], tip="22:00")
    state = board([player(8, "Out", UT, tip="19:30", status="OUT"), player(11, "Filler", BE, tip="19:30"),
                   player(12, "Late", BE, tip="22:00")])
    data = DailyActionsData(lineup=state, nba_date=DAY.isoformat(), can_write=True, actions=[STREAM, late, SCRATCH])
    items = svc.items_of(data, WRITABLE, NOW)
    assert [(i.action.id, i.standing, i.deadline_et) for i in items] == [
        ("scratch:11", "needs_you", "19:30"), ("start:12", "needs_you", "22:00"), ("add_drop:900", "your_call", None)]
    assert items[0].deadline_at == TIP


def _item(standing):
    return SimpleNamespace(standing=standing)


def test_the_teams_state_is_its_most_pressing_item_and_autopilot_outranks_optional_ones():
    assert svc.state_of([_item("your_call"), _item("needs_you")], autopilot=True) == "needs_you"
    assert svc.state_of([_item("your_call"), _item("covered")], autopilot=True) == "autopilot"
    assert svc.state_of([_item("your_call"), _item("covered")], autopilot=False) == "your_call"
    assert svc.state_of([_item("covered")], autopilot=False) == "set"
    assert svc.state_of([], autopilot=False) == "set"


# ------------------------------- tonight ------------------------------- #


def _row(status, fpts=0.0):
    return SimpleNamespace(game_status=status, live_fpts=fpts)


def test_a_players_mark_follows_his_game_then_his_status():
    assert svc.mark_of(player(1, "A", PG, game=False), None, None, NOW) == "idle"
    assert svc.mark_of(player(1, "A", PG, status="OUT", started=True), 2, TIP, NOW) == "out"
    assert svc.mark_of(player(1, "A", PG), 3, TIP, NOW) == "final"
    assert svc.mark_of(player(1, "A", PG), 2, TIP, NOW) == "live"
    assert svc.mark_of(player(1, "A", PG, status="DAY_TO_DAY"), 1, TIP, NOW) == "questionable"
    assert svc.mark_of(player(1, "A", PG), None, TIP, NOW) == "later"


def test_without_a_box_score_a_tipped_game_is_on_until_it_is_surely_over():
    tipped = player(1, "A", PG, started=True)
    assert svc.mark_of(tipped, None, TIP, TIP + timedelta(hours=1)) == "live"
    assert svc.mark_of(tipped, None, TIP, TIP + svc.GAME_SURELY_OVER) == "final"


def test_tonight_lists_the_starting_seats_in_order_with_the_empty_ones():
    state = board([
        player(1, "Guard", PG, tip="19:00", started=True),
        player(2, "Wing", SG, tip="22:00", status="QUESTIONABLE", team="MIA"),
        player(8, "Util", UT, game=False), player(11, "Bench", BE, tip="20:00", team="PHX"),
        player(14, "Hurt", IR, tip="19:00", status="OUT", started=True),
    ])
    rows, teams = svc.game_states([SimpleNamespace(player_id=1, team="DEN", live=_row(2, 21.5)),
                                   SimpleNamespace(player_id=77, team="LAL", live=None)])
    tonight = svc.tonight_of(state, rows, teams, et(19, 40), points_league=True)
    assert [(s.slot, s.name, s.mark) for s in tonight.seats] == [
        ("PG", "Guard", "live"), ("SG", "Wing", "questionable"), ("C", None, "empty"), ("UT", "Util", "idle"),
        ("UT", None, "empty")]
    assert tonight.counts == {"live": 1, "later": 0, "final": 0, "questionable": 1, "out": 0, "idle": 1, "empty": 2}
    assert tonight.points == 21.5 and tonight.seats[0].points == 21.5
    assert [s.name for s in tonight.bench] == ["Bench"] and [(s.name, s.mark) for s in tonight.ir] == [("Hurt", "out")]
    # the bench player's 8 PM tip is the next lock: IR and games already on don't count
    assert tonight.next_lock_et == "20:00" and tonight.next_lock_at == et(20, 0)
    assert tonight.first_tip_et == "19:00"


def test_a_teammates_box_score_says_a_player_with_none_is_playing_and_categories_show_no_points():
    state = board([player(1, "Guard", PG, tip="19:00", started=True, team="BOS")])
    rows, teams = svc.game_states([SimpleNamespace(player_id=55, team="BOS", live=_row(3, 30.0))])
    tonight = svc.tonight_of(state, rows, teams, et(21, 0), points_league=True)
    assert tonight.seats[0].mark == "final" and tonight.seats[0].points is None and tonight.points is None
    rows, teams = svc.game_states([SimpleNamespace(player_id=1, team="BOS", live=_row(2, 30.0))])
    assert svc.tonight_of(state, rows, teams, et(21, 0), points_league=False).points is None


# ------------------------------- the matchup and its week ------------------------------- #


def _mp(pid, slot, team="DEN", status=None, live=None):
    return SimpleNamespace(player_id=pid, lineup_slot=slot, team=team, injury_status=status, live=live)


def _live(fmt="points", cats=None, game_date=DAY.isoformat()):
    yours = [_mp(1, "PG", live=_row(2, 20.0)), _mp(2, "SG", live=_row(3, 15.5)), _mp(3, "BE", live=_row(2, 40.0)),
             _mp(4, "IR", status="OUT"), _mp(5, "UT", live=_row(1, 0.0))]
    theirs = [_mp(21, "PG", live=_row(2, 11.0)), _mp(22, "BE")]
    side = lambda name, score, proj, roster: SimpleNamespace(team_name=name, current_score=score,   # noqa: E731
                                                             projected_score=proj, roster=roster)
    return SimpleNamespace(matchup_period=5, matchup_period_start="2026-11-16", matchup_period_end="2026-11-22",
                           your_team=side("Goblins", 412.5, 980.0, yours),
                           opponent_team=side("Rival", 399.0, 955.0, theirs),
                           scoring_format=fmt, category_comparison=cats, game_date=game_date)


def _past(pid, had_game, slot_id=None):
    return SimpleNamespace(player_id=pid, had_game=had_game, lineup_slot_id=slot_id)


def _future(pid, has_game, status=None):
    return SimpleNamespace(player_id=pid, has_game=has_game, injury_status=status)


def _day(d, kind, yours, theirs, *, totals=(None, None), sources=("snapshot", "snapshot")):
    def team(roster, total, source):
        return SimpleNamespace(roster=roster, total_fpts=total, roster_source=source)

    return SimpleNamespace(date=d, day_type=kind, your_team=team(yours, totals[0], sources[0]),
                           opponent_team=team(theirs, totals[1], sources[1]))


def test_the_matchup_line_carries_the_live_score_and_the_projections_win_chance():
    m = svc.matchup_of(_live(), 0.62)
    assert (m.opponent_name, m.your_score, m.opponent_score, m.win_chance) == ("Rival", 412.5, 399.0, 0.62)
    assert (m.matchup_period, m.start, m.end, m.categories) == (5, "2026-11-16", "2026-11-22", None)
    cats = svc.matchup_of(_live("categories", SimpleNamespace(wins=3, losses=5, ties=1)), None)
    assert (cats.categories.wins, cats.categories.losses, cats.categories.ties, cats.win_chance) == (3, 5, 1, None)


def test_the_week_counts_who_started_who_starts_and_who_could():
    days = [
        # a day with both lineups on file: starters with a game, bench and IR left out
        _day("2026-11-17", "past", [_past(1, True, PG), _past(3, True, BE), _past(5, False, UT)],
             [_past(21, True, PG)], totals=(141.25, 120.0)),
        # a day counted on today's roster: everyone with a game, capped at the seats
        _day("2026-11-18", "past", [_past(n, True) for n in range(1, 8)], [_past(21, True)],
             totals=(150.0, 90.0), sources=("current", "snapshot")),
        _day("2026-11-19", "today", [_past(1, True), _past(2, True), _past(3, True), _past(5, False)],
             [_past(21, True), _past(22, True)]),
        _day("2026-11-20", "future", [_future(1, True), _future(2, True, "OUT"), _future(3, True), _future(4, True),
                                      _future(5, False)], [_future(21, True), _future(22, True)]),
    ]
    week = svc.week_of(days, _live(), seats=5)
    assert [(d.kind, d.your_points, d.opponent_points, d.your_games, d.opponent_games, d.estimated) for d in week] == [
        ("past", 141.2, 120.0, 1, 1, False),
        ("past", 150.0, 90.0, 5, 1, True),
        ("today", 35.5, 11.0, 2, 1, False),        # starters only: the bench's 40 doesn't count
        ("future", None, None, 2, 2, False),       # the OUT player and the one on IR can't play
    ]


def test_box_scores_from_an_earlier_day_are_not_todays_points():
    """While the matchup's baseline lags ESPN's day, the live matchup overlays the day before."""
    days = [_day("2026-11-18", "past", [_past(1, True, PG)], [_past(21, True, PG)], totals=(150.0, 90.0)),
            _day("2026-11-19", "today", [_past(1, True), _past(2, True)], [_past(21, True)])]
    week = svc.week_of(days, _live(game_date="2026-11-18"), seats=5)
    assert [(d.kind, d.your_points, d.opponent_points, d.your_games) for d in week] == [
        ("past", 150.0, 90.0, 1), ("today", None, None, 2)]


def test_a_categories_week_has_games_and_no_points():
    days = [_day("2026-11-17", "past", [_past(1, True, PG)], [_past(21, True, PG)], totals=(141.0, 120.0)),
            _day("2026-11-19", "today", [_past(1, True)], [_past(21, True)])]
    week = svc.week_of(days, _live("categories"), seats=5)
    assert [(d.your_points, d.opponent_points, d.your_games) for d in week] == [(None, None, 1), (None, None, 1)]


def test_starting_seats_leave_out_the_bench_and_ir():
    assert svc.starting_seats(board([])) == 5 and svc.starting_seats(None) is None


# ------------------------------- the season ------------------------------- #

PROJECTION = {
    "teams_count": 10, "playoff_teams": 6,
    "teams": [{"mine": False, "wins": 5, "losses": 0, "ties": 0, "standing": 1, "playoff_odds": 0.99},
              {"mine": True, "wins": 3, "losses": 2, "ties": 0, "standing": 4, "playoff_odds": 0.81}],
    "matchups": [{"matchup": 4, "status": "final", "win_chance": None},
                 {"matchup": 5, "status": "current", "win_chance": 0.64},
                 {"matchup": 6, "status": "upcoming", "win_chance": 0.5}],
}


def test_the_season_line_is_the_users_own_team_and_this_matchups_win_chance():
    season, win = svc.season_of(PROJECTION, NOW, 5)
    assert (season.wins, season.losses, season.standing, season.teams, season.playoff_teams) == (3, 2, 4, 10, 6)
    assert season.playoff_odds == 0.81 and win == 0.64 and season.as_of == NOW
    # a projection kept across the turn of a matchup period: its odds are the last matchup's, not this one's
    season, win = svc.season_of(PROJECTION, NOW, 6)
    assert season.wins == 3 and win is None
    assert svc.season_of(PROJECTION, NOW)[1] is None                     # and no matchup now has none
    assert svc.season_of({"teams": [{"mine": False}], "matchups": []}, NOW) == (None, None)


# ------------------------------- what's next ------------------------------- #


def _pickup(at, add="A. Guard", drop="B. Wing", retry=None):
    return SimpleNamespace(not_before_at=at, next_attempt_at=retry, add_name=add, drop_name=drop)


def _lane(at, status="active"):
    return SimpleNamespace(next_decision_at=at, status=status, holder=SimpleNamespace(name="C. Center"))


def test_next_is_the_soonest_thing_scheduled():
    slot = SimpleNamespace(kind="pre_lock", due_at=et(18, 15))
    nxt = svc.next_of(NOW, fill_at=et(18, 0), pickups=[_pickup(et(3, 5, DAY + timedelta(days=1)))],
                      lanes=[_lane(et(17, 30)), _lane(et(17, 5), "paused")], checkin=slot)
    assert (nxt.kind, nxt.at, nxt.label) == ("lane", et(17, 30), "Stream lane decides on C. Center's spot")
    assert svc.next_of(NOW, checkin=slot).label == "Pre-lock check-in"
    assert svc.next_of(NOW) is None


def test_something_already_due_is_due_now_and_a_stored_time_without_a_zone_is_utc():
    naive = (NOW - timedelta(minutes=5)).replace(tzinfo=None)
    nxt = svc.next_of(NOW, pickups=[_pickup(naive, drop=None)])
    assert (nxt.kind, nxt.at, nxt.label) == ("pickup", NOW, "Add A. Guard")
    retry = svc.next_of(NOW, pickups=[_pickup(NOW - timedelta(hours=1), retry=NOW + timedelta(minutes=9))])
    assert retry.at == NOW + timedelta(minutes=9) and retry.label == "Add A. Guard, drop B. Wing"
    assert svc.next_of(NOW, fill_at=et(16, 0)).at == NOW


# ------------------------------- a team's rules, from its rows ------------------------------- #


def _prefs(**kw):
    base = dict(lineup_alerts_enabled=True, auto_lineup_enabled=True, alert_minutes_before=60, late_scratch_level="act")
    return SimpleNamespace(**{**base, **kw})


def autopilot_row(*, switch=True, rows=(), paused_at=None):
    """A usr.autopilot row as the settings read it."""
    return SimpleNamespace(autopilot=switch, ai_rows=list(rows), paused_at=paused_at, paused_by=None,
                           paused_reason=None, active_since=NOW - timedelta(days=3))


def _write(source, status, trigger=None):
    return SimpleNamespace(source=source, status=status, trigger_player_id=trigger)


def account(*, prefs=None, team_prefs=None, autopilot=None, dime=False, calendar=None, now=NOW, tip=et(19, 0)):
    return svc.Account(user_id=11, now=now, today=DAY, tip_at=tip, prefs=prefs, team_prefs=team_prefs or {},
                       autopilot=autopilot or {}, dime=dime, calendar=calendar)


def _rules(acct, plan, *, can_write=True, board_date=DAY.isoformat()):
    return svc.rules_of(23, acct, plan, svc.levels_of(23, acct), can_write=can_write, board_date=board_date)


def test_a_teams_levels_come_from_its_preferences():
    view = svc.levels_of(23, account(prefs=_prefs()))
    assert view == {"lineup": "act", "daily_actions": "off", "lanes": "act", "late_scratch": "act", "checkins": "off"}
    assert svc.levels_of(23, account())["lineup"] == "off"                  # no preferences: nothing runs


def test_the_fill_counts_only_on_a_day_it_can_still_run():
    acct = account(prefs=_prefs())
    rules = _rules(acct, svc._Plan())
    assert rules.fill_applies and not rules.fill_ran and rules.fill_closes_at == et(18, 45)
    assert rules.late_scratch == "act" and rules.dime_acts == frozenset()
    assert _rules(acct, svc._Plan(writes=[_write("auto", "noop")])).fill_ran
    failed = _rules(acct, svc._Plan(writes=[_write("auto", "failed")]))
    assert failed.fill_failed and not failed.fill_ran
    retried = _rules(acct, svc._Plan(writes=[_write("auto", "rejected"), _write("auto", "applied")]))
    assert retried.fill_ran and not retried.fill_failed
    # whether it ran is unknown without the plan rows, and a board on another day isn't today's fill
    assert not _rules(acct, None).fill_applies
    assert not _rules(acct, svc._Plan(), board_date="2026-11-18").fill_applies
    assert not _rules(account(prefs=_prefs(auto_lineup_enabled=False)), svc._Plan()).fill_applies
    assert _rules(account(prefs=_prefs(), tip=None), svc._Plan()).fill_closes_at is None


def test_a_swap_that_failed_is_remembered_by_player_until_one_lands():
    plan = svc._Plan(writes=[_write("availability", "failed", 80), _write("availability", "rejected", 90),
                             _write("availability", "applied", 90)])
    rules = _rules(account(prefs=_prefs()), plan)
    assert rules.scratch_failed == frozenset({80}) and rules.scratch_answered == frozenset({90})
    assert not rules.scratch_spent


def test_the_days_swaps_are_spent_at_the_late_scratch_services_own_limit(monkeypatch):
    monkeypatch.setattr(svc.settings, "late_scratch_swaps_per_day", 2)
    one = [_write("availability", "applied", 90), _write("availability", "failed", 80), _write("auto", "applied")]
    assert not _rules(account(prefs=_prefs()), svc._Plan(writes=one)).scratch_spent
    two = [*one, _write("availability", "applied_unverified", 70)]
    rules = _rules(account(prefs=_prefs()), svc._Plan(writes=two))
    assert rules.scratch_spent and rules.scratch_answered == frozenset({90, 70})


def _ruling(nba_id, changed_at):
    return SimpleNamespace(nba_player_id=nba_id, changed_at=changed_at)


def test_rulings_are_read_only_where_an_automation_answers_them(monkeypatch):
    asked = []

    async def today(ids, day):
        asked.append((list(ids), day))
        return [_ruling(80, NOW.replace(tzinfo=None)), _ruling(81, None)]

    monkeypatch.setattr(svc.AvailabilityService, "today", staticmethod(today))
    data = _actions(SCRATCH, START).data
    act = svc.Rules(can_write=True, late_scratch=autonomy.ACT)
    assert asyncio.run(svc._ruled_at(23, data, act)) == {80: NOW.replace(tzinfo=timezone.utc)}
    assert asked == [([80], DAY)]                    # the starter the scratch benches, on the board's day
    dime = svc.Rules(can_write=True, dime_acts=frozenset({autonomy.LATE_SCRATCH}))
    assert 80 in asyncio.run(svc._ruled_at(23, data, dime))
    # nobody answers rulings at notify, and a day without a scratch has none to look up
    assert asyncio.run(svc._ruled_at(23, data, svc.Rules(late_scratch=autonomy.NOTIFY))) == {}
    assert asyncio.run(svc._ruled_at(23, _actions(START).data, act)) == {}
    assert len(asked) == 2


def test_rulings_that_cannot_be_read_leave_nothing_counted_on(monkeypatch):
    async def today(ids, day):
        raise RuntimeError("no database")

    monkeypatch.setattr(svc.AvailabilityService, "today", staticmethod(today))
    act = svc.Rules(can_write=True, late_scratch=autonomy.ACT)
    assert asyncio.run(svc._ruled_at(23, _actions(SCRATCH).data, act)) == {}


def test_dime_acts_only_with_access_and_its_check_ins_on(monkeypatch):
    monkeypatch.setattr(svc.settings, "ai_enabled", True)
    monkeypatch.setattr(svc.settings, "autopilot_enabled", True)
    on = autopilot_row()
    slot = SimpleNamespace(kind="pre_lock", due_at=et(18, 15))
    rules = _rules(account(prefs=_prefs(), autopilot={23: on}, dime=True), svc._Plan(checkin=slot))
    assert rules.dime_acts == frozenset(autonomy.AI_ROWS) and rules.next_checkin_at == et(18, 15)
    assert _rules(account(prefs=_prefs(), autopilot={23: on}, dime=False), svc._Plan()).dime_acts == frozenset()
    paused = autopilot_row(paused_at=NOW)
    assert _rules(account(prefs=_prefs(), autopilot={23: paused}, dime=True), svc._Plan()).dime_acts == frozenset()


def test_the_auto_lineup_is_next_only_while_it_is_still_to_run():
    acct = account(prefs=_prefs())
    assert svc._fill_at(acct, _rules(acct, svc._Plan()), 23) == et(18, 0)          # an hour before the first tip
    assert svc._fill_at(acct, _rules(acct, svc._Plan(writes=[_write("auto", "applied")])), 23) is None
    late = account(prefs=_prefs(), now=et(18, 50))
    assert svc._fill_at(late, _rules(late, svc._Plan()), 23) is None
    # a fill that failed today is retried, but it isn't counted on for the items, so it isn't promised as next
    assert svc._fill_at(acct, _rules(acct, svc._Plan(writes=[_write("auto", "failed")])), 23) is None
    assert svc._fill_at(acct, _rules(acct, svc._Plan(), can_write=False), 23) is None


# ------------------------------- the whole read ------------------------------- #


def team_ctx(team_id, name="Goblins", provider="espn", scoring="points", league_name="League One"):
    league = SimpleNamespace(scoring_type=scoring, scoring_preview=None, name=league_name, roster_slots={})
    stored = f'{{"provider": "{provider}", "team_name": "{name}", "league_name": "N/A", "league_id": 1, "year": 2027}}'
    return SimpleNamespace(team_id=team_id, user_id=11, league_info_json=stored, league_id=1, league=league)


def _actions(*actions, state=None, can_write=True):
    state = state if state is not None else board([player(8, "Idle", UT, game=False), player(11, "Filler", BE)])
    return DailyActionsResp(status=ApiStatus.SUCCESS, message="ok", data=DailyActionsData(
        lineup=state, roster_version="v1", scoring_period_id=31, nba_date=DAY.isoformat(), can_write=can_write,
        actions=list(actions)))


class Harness:
    """The reads behind `overview`, as fakes a test fills in by team id."""

    def __init__(self, monkeypatch, teams, acct):
        self.boards, self.matchups, self.plans, self.seasons = {}, {}, {}, {}
        self.rulings = []
        self.builds = 0

        async def direct(_name, fn, *a, **kw):
            return fn(*a, **kw)

        async def the_account(user_id, team_ids, now):
            self.builds += 1
            return acct

        async def creds(team):
            return ESPN.model_copy(update={"provider": FantasyProvider(team_provider(team))})

        async def read(team, league_info):
            got = self.boards[team.team_id]
            if isinstance(got, Exception):
                raise got
            return got

        async def matchup(team):
            got = self.matchups.get(team.team_id)
            if isinstance(got, Exception):
                raise got
            return got

        async def plan(team, _acct):
            got = self.plans.get(team.team_id, svc._Plan())
            if isinstance(got, Exception):
                raise got
            return got

        async def season(team, league_info):
            return self.seasons.get(team.team_id, ("unavailable", None))

        async def rulings(ids, day):
            return [r for r in self.rulings if r.nba_player_id in ids]

        monkeypatch.setattr(svc, "run_db", direct)
        monkeypatch.setattr(svc, "_owned_teams", lambda user_id: list(teams))
        monkeypatch.setattr(svc, "_account", the_account)
        monkeypatch.setattr(svc, "load_owned_league_info", creds)
        monkeypatch.setattr(svc.DailyActionsService, "read", staticmethod(read))
        monkeypatch.setattr(svc, "_matchup", matchup)
        monkeypatch.setattr(svc, "_plan", plan)
        monkeypatch.setattr(svc, "_season", season)
        monkeypatch.setattr(svc.AvailabilityService, "today", staticmethod(rulings))

    def overview(self, now=NOW):
        return asyncio.run(svc.overview(11, now))


def team_provider(team):
    import json
    return json.loads(team.league_info_json)["provider"]


def test_one_team_that_cannot_be_read_never_fails_the_others(monkeypatch):
    h = Harness(monkeypatch, [team_ctx(21, "Finest"), team_ctx(23), team_ctx(24, "Minomjep")], account())
    h.boards = {21: ProviderAuthError("espn"), 23: _actions(START), 24: _actions()}
    h.matchups = {21: svc._MatchupRead(live=_live(), days=[])}
    data = h.overview()
    by_id = {t.team_id: t for t in data.teams}
    assert [t.team_id for t in data.teams] == [23, 21, 24]            # needs you, then can't read, then set
    assert data.counts == {"needs_you": 1, "your_call": 0, "set": 1, "autopilot": 0, "cant_read": 1}
    broken = by_id[21]
    assert broken.state == "cant_read" and broken.error.code == "PROVIDER_AUTH_EXPIRED"
    assert broken.team_name == "Finest" and broken.league_name == "League One" and broken.items == []
    assert broken.matchup.your_score == 412.5                          # what could be read still shows
    assert by_id[23].state == "needs_you" and by_id[23].items[0].deadline_et == "19:30"
    assert data.next_deadline_at == TIP and by_id[24].state == "set"


def test_a_team_carries_its_board_for_staging_and_names_what_could_not_be_read(monkeypatch):
    h = Harness(monkeypatch, [team_ctx(23)], account(prefs=_prefs()))
    h.boards = {23: _actions(START, STREAM)}
    h.matchups = {23: RuntimeError("espn fell over")}
    h.plans = {23: RuntimeError("no database")}
    team = h.overview().teams[0]
    # whether the fill ran is unknown without the plan rows, so the start is the user's
    assert team.state == "needs_you" and [i.standing for i in team.items] == ["needs_you", "your_call"]
    assert team.missing == ["matchup", "next"] and team.matchup is None and team.week is None and team.next is None
    assert (team.nba_date, team.roster_version, team.scoring_period_id) == (DAY.isoformat(), "v1", 31)
    assert team.can_write is True
    assert team.automation.lineup == "act" and team.automation.dime is False
    assert team.tonight.counts["idle"] == 1 and team.scoring_format == "points"


def test_a_team_whose_automations_cover_the_day_is_set_and_says_what_is_next(monkeypatch):
    h = Harness(monkeypatch, [team_ctx(23)], account(prefs=_prefs()))
    h.boards = {23: _actions(START)}
    h.matchups = {23: svc._MatchupRead(live=_live(), days=None)}
    h.seasons = {23: ("ready", svc._Season(PROJECTION, NOW))}
    team = h.overview().teams[0]
    assert team.state == "set" and team.items[0].covered_by == "auto_lineup"
    assert (team.next.kind, team.next.at) == ("auto_lineup", et(18, 0))
    assert team.missing == ["week"] and team.week is None and team.matchup.win_chance == 0.64
    assert team.season_state == "ready" and (team.season.wins, team.season.standing) == (3, 4)


def test_a_ruling_is_covered_while_the_swap_is_on_its_way_and_the_users_once_it_is_not(monkeypatch):
    h = Harness(monkeypatch, [team_ctx(23)], account(prefs=_prefs()))
    h.boards = {23: _actions(SCRATCH, state=board([player(8, "Out", UT), player(11, "Filler", BE)]))}
    h.plans = {23: svc._Plan(writes=[_write("auto", "applied")])}         # the day's fill has run
    h.rulings = [_ruling(80, NOW - timedelta(minutes=1))]
    team = h.overview().teams[0]
    assert team.state == "set" and team.items[0].covered_by == "late_scratch"
    # the same scratch, ruled before the fill ran: the swap answered then, and didn't make it
    svc.clear()
    h.rulings = [_ruling(80, NOW - timedelta(hours=2))]
    team = h.overview().teams[0]
    assert team.state == "needs_you" and team.items[0].covered_by is None and team.items[0].deadline_et == "19:30"


def test_box_scores_from_an_earlier_day_do_not_mark_tonights_seats(monkeypatch):
    """The live matchup overlays the day before while its baseline lags: tonight is read off the board."""
    h = Harness(monkeypatch, [team_ctx(23)], account())
    h.boards = {23: _actions(state=board([player(1, "Guard", PG, tip="19:00")]))}
    h.matchups = {23: svc._MatchupRead(live=_live(game_date="2026-11-18"), days=[])}
    tonight = h.overview().teams[0].tonight
    assert (tonight.seats[0].mark, tonight.seats[0].points, tonight.points) == ("later", None, None)
    svc.clear()
    h.matchups = {23: svc._MatchupRead(live=_live(), days=[])}             # the same box scores, on the board's day
    tonight = h.overview().teams[0].tonight
    assert (tonight.seats[0].mark, tonight.seats[0].points, tonight.points) == ("live", 20.0, 20.0)


def test_autopilot_is_the_state_of_a_team_dime_runs(monkeypatch):
    monkeypatch.setattr(svc.settings, "ai_enabled", True)
    monkeypatch.setattr(svc.settings, "autopilot_enabled", True)
    on = autopilot_row()
    h = Harness(monkeypatch, [team_ctx(30, "Dime's Team")], account(prefs=_prefs(), autopilot={30: on}, dime=True))
    h.boards = {30: _actions(STREAM)}
    team = h.overview().teams[0]
    assert team.state == "autopilot" and team.items[0].covered_by == "dime"
    assert team.automation.autopilot and team.automation.dime and team.automation.daily_actions == "ai"


def test_a_provider_without_lineups_shows_its_score_and_says_why_it_has_no_state(monkeypatch):
    h = Harness(monkeypatch, [team_ctx(40, "Yahoo Team", provider="yahoo")], account())
    h.boards = {40: DailyActionsResp(status=ApiStatus.SUCCESS, message="n/a", data=DailyActionsData(
        can_write=False, write_blocked_reason="provider_not_supported"))}
    h.matchups = {40: svc._MatchupRead(live=_live(), days=[])}
    team = h.overview().teams[0]
    assert team.state == "cant_read" and team.error.code == "LINEUP_NOT_SUPPORTED"
    assert "Yahoo" in team.error.message and team.provider == FantasyProvider.YAHOO
    assert team.matchup.opponent_name == "Rival" and team.tonight is None


def test_a_stored_row_that_is_not_an_object_still_names_the_team():
    team = team_ctx(23)
    team.league_info_json = "null"              # free text in the column: valid JSON, no fields
    assert svc._names(team)["team_id"] == 23 and svc._names(team)["provider"] == FantasyProvider.ESPN
    team.league_info_json = "not json"
    assert svc._names(team)["team_name"] == "Team 23"


def test_a_team_whose_credentials_cannot_be_read_still_has_its_name(monkeypatch):
    h = Harness(monkeypatch, [team_ctx(23)], account())

    async def broken(team):
        raise RuntimeError("vault down")

    monkeypatch.setattr(svc, "load_owned_league_info", broken)
    team = h.overview().teams[0]
    assert (team.state, team.error.code, team.team_name) == ("cant_read", "TEAM_READ_FAILED", "Goblins")
    assert "vault" not in team.error.message                       # never the raw exception


def test_an_account_without_teams_answers_empty(monkeypatch):
    h = Harness(monkeypatch, [], account())
    data = h.overview()
    assert data.teams == [] and sum(data.counts.values()) == 0 and h.builds == 0


def test_the_answer_is_kept_briefly_and_never_across_a_write(monkeypatch):
    h = Harness(monkeypatch, [team_ctx(23)], account())
    h.boards = {23: _actions()}
    first = h.overview()
    assert h.overview() is first and h.builds == 1
    inflight._bump_write_epoch()                                    # a move sent from a desk
    assert h.overview() is not first and h.builds == 2
    kept = svc._overviews._rows[11]
    svc._overviews._rows[11] = svc._Kept(kept.value, kept.at - svc.OVERVIEW_TTL - 1, kept.epoch, kept.ttl)
    h.overview()                                                    # older than the keep: read again
    assert h.builds == 3


def test_two_reads_at_once_share_one_build(monkeypatch):
    h = Harness(monkeypatch, [team_ctx(23)], account())
    h.boards = {23: _actions()}

    async def both():
        return await asyncio.gather(svc.overview(11, NOW), svc.overview(11, NOW))

    a, b = asyncio.run(both())
    assert a is b and h.builds == 1


# ------------------------------- the matchup read ------------------------------- #


def test_the_matchup_is_read_from_the_provider_once_and_its_week_is_kept(monkeypatch):
    calls = {"provider": 0, "weekly": 0, "reuse": []}
    md = SimpleNamespace(matchup_period=5)

    async def provider(user_id, team_id, avg_window="season"):
        calls["provider"] += 1
        return SimpleNamespace(status=ApiStatus.SUCCESS, data=md)

    async def live(user_id, team_id, reuse_from=None):
        calls["reuse"].append(reuse_from)
        return SimpleNamespace(status=ApiStatus.SUCCESS, data=_live())

    async def weekly(team_id, matchup):
        calls["weekly"] += 1
        return SimpleNamespace(data=SimpleNamespace(days=["mon", "tue"]))

    monkeypatch.setattr(svc.MatchupService, "get_matchup_by_team_id", staticmethod(provider))
    monkeypatch.setattr(svc.MatchupService, "get_live_matchup_by_team_id", staticmethod(live))
    monkeypatch.setattr(svc.MatchupService, "weekly_of", staticmethod(weekly))
    team = team_ctx(23)
    first = asyncio.run(svc._matchup(team))
    second = asyncio.run(svc._matchup(team))
    assert first.days == second.days == ["mon", "tue"] and calls["weekly"] == 1
    # the live score is laid over the read just made, not over a second provider read
    assert calls["provider"] == 2 and all(isinstance(r, int) for r in calls["reuse"])
    inflight._bump_write_epoch()
    asyncio.run(svc._matchup(team))
    assert calls["weekly"] == 2
    # ESPN's day turns inside the same matchup period: the kept week still calls yesterday today
    monkeypatch.setattr(svc, "matchup_today", lambda matchup: DAY + timedelta(days=1))
    asyncio.run(svc._matchup(team))
    asyncio.run(svc._matchup(team))
    assert calls["weekly"] == 3


def test_a_week_is_not_laid_beside_another_matchup_periods_score(monkeypatch):
    """A write between the two reads sends the live score back to the provider, which may have moved on."""
    built = []

    async def provider(user_id, team_id, avg_window="season"):
        return SimpleNamespace(status=ApiStatus.SUCCESS, data=SimpleNamespace(matchup_period=4))

    async def live(user_id, team_id, reuse_from=None):
        return SimpleNamespace(status=ApiStatus.SUCCESS, data=_live())           # matchup period 5

    async def weekly(team_id, matchup):
        built.append(matchup.matchup_period)
        return SimpleNamespace(data=SimpleNamespace(days=["mon"]))

    monkeypatch.setattr(svc.MatchupService, "get_matchup_by_team_id", staticmethod(provider))
    monkeypatch.setattr(svc.MatchupService, "get_live_matchup_by_team_id", staticmethod(live))
    monkeypatch.setattr(svc.MatchupService, "weekly_of", staticmethod(weekly))
    read = asyncio.run(svc._matchup(team_ctx(23)))
    assert read.live.matchup_period == 5 and read.days is None and built == []


def test_no_matchup_is_no_block_and_a_week_that_fails_leaves_the_score(monkeypatch):
    async def none(user_id, team_id, avg_window="season"):
        return SimpleNamespace(status=ApiStatus.SUCCESS, data=None)

    monkeypatch.setattr(svc.MatchupService, "get_matchup_by_team_id", staticmethod(none))
    assert asyncio.run(svc._matchup(team_ctx(23))) is None

    async def some(user_id, team_id, avg_window="season"):
        return SimpleNamespace(status=ApiStatus.SUCCESS, data=SimpleNamespace(matchup_period=5))

    async def live(user_id, team_id, reuse_from=None):
        return SimpleNamespace(status=ApiStatus.SUCCESS, data=_live())

    async def weekly(team_id, matchup):
        raise RuntimeError("no box scores")

    monkeypatch.setattr(svc.MatchupService, "get_matchup_by_team_id", staticmethod(some))
    monkeypatch.setattr(svc.MatchupService, "get_live_matchup_by_team_id", staticmethod(live))
    monkeypatch.setattr(svc.MatchupService, "weekly_of", staticmethod(weekly))
    read = asyncio.run(svc._matchup(team_ctx(23)))
    assert read.live.matchup_period == 5 and read.days is None


# ------------------------------- the league's projection ------------------------------- #


def test_a_league_without_projections_is_never_asked(monkeypatch):
    asked = []

    async def never(league_info):
        asked.append(league_info)        # recorded, since a raise here would only read as unavailable too
        raise LeagueProjectionUnavailable("NOT_ASKED", "not asked")

    monkeypatch.setattr(svc, "league_projection", never)
    assert asyncio.run(svc._season(team_ctx(21, scoring="categories"), ESPN)) == ("unavailable", None)
    yahoo = ESPN.model_copy(update={"provider": FantasyProvider.YAHOO})
    assert asyncio.run(svc._season(team_ctx(40), yahoo)) == ("unavailable", None)
    assert asked == []


def test_a_projection_is_kept_and_one_that_cannot_be_made_is_not_asked_again(monkeypatch):
    calls = []

    async def project(league_info):
        calls.append(league_info.league_id)
        if league_info.league_id == 2:
            raise LeagueProjectionUnavailable("LEAGUE_PROJECTION_POINTS_ONLY", "points only")
        return PROJECTION

    monkeypatch.setattr(svc, "league_projection", project)

    async def twice(team, info):
        return await svc._season(team, info), await svc._season(team, info)

    (state, held), (again, _) = asyncio.run(twice(team_ctx(23), ESPN))
    assert state == again == "ready" and held.projection is PROJECTION and calls == [1]
    other = ESPN.model_copy(update={"league_id": 2})
    assert asyncio.run(twice(team_ctx(24, scoring=None), other)) == (("unavailable", None), ("unavailable", None))
    assert calls == [1, 2]


def test_a_read_does_not_wait_long_for_a_projection_and_the_next_one_has_it(monkeypatch):
    monkeypatch.setattr(svc, "SEASON_BUDGET", 0.01)
    release = None

    async def slow(league_info):
        await release.wait()
        return PROJECTION

    monkeypatch.setattr(svc, "league_projection", slow)

    async def scenario():
        nonlocal release
        release = asyncio.Event()
        first = await svc._season(team_ctx(23), ESPN)
        release.set()
        await asyncio.sleep(0.01)
        return first, await svc._season(team_ctx(23), ESPN)

    first, second = asyncio.run(scenario())
    assert first == ("pending", None) and second[0] == "ready"


def test_a_refresh_that_fails_keeps_the_projection_it_was_replacing(monkeypatch):
    outcomes = [PROJECTION, RuntimeError("espn fell over")]

    async def project(league_info):
        got = outcomes.pop(0)
        if isinstance(got, Exception):
            raise got
        return got

    monkeypatch.setattr(svc, "league_projection", project)

    async def scenario():
        await svc._season(team_ctx(23), ESPN)
        kept = svc._seasons._rows[23]                               # make the kept one old
        svc._seasons._rows[23] = svc._Kept(kept.value, kept.at - svc.SEASON_TTL - 1, kept.epoch, kept.ttl)
        return await svc._season(team_ctx(23), ESPN)

    state, held = asyncio.run(scenario())
    assert state == "ready" and held.projection is PROJECTION and outcomes == []


def test_a_first_projection_that_fails_is_unavailable_until_the_retry(monkeypatch):
    async def project(league_info):
        raise RuntimeError("espn fell over")

    monkeypatch.setattr(svc, "league_projection", project)
    assert asyncio.run(svc._season(team_ctx(23), ESPN)) == ("unavailable", None)
    assert svc._seasons._rows[23].ttl == svc.SEASON_RETRY


def test_projections_run_one_league_at_a_time(monkeypatch):
    running, peak = 0, 0

    async def project(league_info):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return PROJECTION

    monkeypatch.setattr(svc, "league_projection", project)

    async def scenario():
        return await asyncio.gather(*(svc._season(team_ctx(n), ESPN) for n in (21, 23, 24)))

    assert [s for s, _ in asyncio.run(scenario())] == ["ready"] * 3 and peak == 1


# ------------------------------- the real shapes ------------------------------- #


def test_the_rules_read_the_services_own_models():
    """The fakes above stand in for these: one pass over the real schemas, so a renamed field
    fails here and not in production."""
    from schemas.matchup import (DailyMatchupData, DailyMatchupFuturePlayer, DailyMatchupPlayerStats,
                                 DailyMatchupTeam, LiveMatchupData, LiveMatchupPlayer, LiveMatchupTeam,
                                 PlayerLiveStats)
    from schemas.stream_lane import LanePlayer, StreamLane
    from services.ai import checkins

    def stats(fpts, status):
        return PlayerLiveStats(nba_player_id=1, live_fpts=fpts, live_pts=0, live_reb=0, live_ast=0, live_stl=0,
                               live_blk=0, live_tov=0, live_min=0, game_status=status)

    def rostered(pid, slot, live=None, status=None):
        return LiveMatchupPlayer(player_id=pid, name=f"P{pid}", team="DEN", position="PG", lineup_slot=slot,
                                 avg_points=30.0, projected_points=30.0, games_remaining=2,
                                 injured=status == "OUT", injury_status=status, live=live)

    def side(name, score, roster):
        return LiveMatchupTeam(team_name=name, team_id=1, current_score=score, projected_score=900.0, roster=roster)

    live = LiveMatchupData(
        matchup_period=5, matchup_period_start="2026-11-16", matchup_period_end="2026-11-22",
        your_team=side("Goblins", 412.5, [rostered(1, "PG", stats(20.0, 2)), rostered(3, "BE", stats(40.0, 2)),
                                          rostered(4, "IR", status="OUT")]),
        opponent_team=side("Rival", 399.0, [rostered(21, "PG", stats(11.0, 3))]),
        projected_winner="Goblins", projected_margin=25.0, game_date=DAY.isoformat(),
    )

    def played(pid, slot_id=None):
        return DailyMatchupPlayerStats(player_id=pid, name=f"P{pid}", team="DEN", position="PG", had_game=True,
                                       lineup_slot_id=slot_id, fpts=30.0)

    def ahead(pid, status=None):
        return DailyMatchupFuturePlayer(player_id=pid, name=f"P{pid}", team="DEN", position="PG", has_game=True,
                                        injured=status == "OUT", injury_status=status)

    def day(d, kind, yours, theirs, total=None, source="current"):
        common = dict(date=d, day_type=kind, day_of_week="Thu", day_index=0, matchup_period=5,
                      matchup_period_start="2026-11-16", matchup_period_end="2026-11-22")
        return DailyMatchupData(
            **common,
            your_team=DailyMatchupTeam(team_name="Goblins", team_id=1, total_fpts=total, roster=yours,
                                       roster_source=source),
            opponent_team=DailyMatchupTeam(team_name="Rival", team_id=2, total_fpts=total, roster=theirs,
                                           roster_source=source))

    week = svc.week_of([day("2026-11-18", "past", [played(1, PG), played(3, BE)], [played(21, PG)], 88.0, "snapshot"),
                        day("2026-11-19", "today", [played(1), played(3)], [played(21)]),
                        day("2026-11-20", "future", [ahead(1), ahead(3), ahead(4, "OUT")], [ahead(21)])],
                       live, seats=5)
    assert [(d.kind, d.your_points, d.your_games, d.opponent_games) for d in week] == [
        ("past", 88.0, 1, 1), ("today", 20.0, 1, 1), ("future", None, 2, 1)]
    assert svc.matchup_of(live, None).opponent_name == "Rival"
    rows, teams = svc.game_states([*live.your_team.roster, *live.opponent_team.roster])
    assert rows[1].live_fpts == 20.0 and teams == {"DEN": 3}

    center = LanePlayer(player_id=5, name="C. Center")
    lane = StreamLane(id=1, team_id=23, status="active", notify="email", holder=center, original=center,
                      start_period=30, end_period=36, end_date=date(2026, 11, 22), next_decision_at=et(17, 30),
                      created_at=NOW)
    slot = checkins.Slot(kind=checkins.PRE_LOCK, nba_date=DAY, due_at=et(18, 15), closes_at=et(18, 50))
    assert svc.next_of(NOW, lanes=[lane], checkin=slot).label == "Stream lane decides on C. Center's spot"
    assert svc.next_of(NOW, checkin=slot).label == "Pre-lock check-in"
