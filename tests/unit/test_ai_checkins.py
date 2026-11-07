"""
Dime's check-ins in shadow (the Dime autopilot plan, step 3): the schedule (which
check-ins a day owes, when each is due, missed or not counted), the tick that
starts them, the brief each one opens with, the calls they log, and the
automations they read. The DB is stubbed here; the claims, the decisions and the
spend line run against the real schema in the integration tests.
"""

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from core.errors import AppError, AuthorizationError, ConflictError, ServiceUnavailableError
from core.nba_calendar import EASTERN
from core.settings import settings
from services.ai import agent, automations, checkins, conversations, decisions, proposals, spend
from services.scheduled_pickup_service import et_at

pytestmark = [pytest.mark.unit]

TUE = date(2026, 11, 10)
MON = date(2026, 11, 9)


def et(d, hh, mm=0):
    return EASTERN.localize(datetime.combine(d, time(hh, mm))).astimezone(timezone.utc)


def run(coro):
    return asyncio.run(coro)


# ------------------------------- the schedule ------------------------------- #


class TestSlots:
    def test_a_game_day_owes_last_night_s_follow_up_a_morning_and_pre_lock(self):
        slots = checkins.slots_for(TUE, time(19), time(19, 30), matchup_start=MON)
        assert [(s.kind, s.nba_date) for s in slots] == [("night", MON), ("morning", TUE), ("pre_lock", TUE)]
        night, morning, pre_lock = slots
        assert (night.due_at, night.closes_at) == (et(TUE, 6), et(TUE, 12))
        assert (morning.due_at, morning.closes_at) == (et(TUE, 9), et(TUE, 14))
        assert (pre_lock.due_at, pre_lock.closes_at) == (et(TUE, 18, 15), et(TUE, 18, 50))

    def test_a_matchup_s_first_day_is_the_weekly_check_in(self):
        kinds = [s.kind for s in checkins.slots_for(MON, time(19), None, matchup_start=MON)]
        assert kinds == ["weekly", "pre_lock"]

    def test_no_games_means_no_pre_lock_and_no_follow_up_the_next_morning(self):
        assert [s.kind for s in checkins.slots_for(TUE, None, None, matchup_start=MON)] == ["morning"]
        # Out of the season nothing is owed at all
        assert checkins.slots_for(TUE, None, None, matchup_start=None) == []

    def test_an_early_tip_closes_the_morning_before_pre_lock_opens(self):
        slots = {s.kind: s for s in checkins.slots_for(TUE, time(12), None, matchup_start=MON)}
        assert slots["morning"].closes_at == slots["pre_lock"].due_at == et(TUE, 11, 15)
        # A tip so early the morning would close before it opens: only pre-lock
        assert [s.kind for s in checkins.slots_for(TUE, time(9, 30), None, matchup_start=MON)] == ["pre_lock"]


class TestStanding:
    slot = checkins.Slot("morning", TUE, et(TUE, 9), et(TUE, 14))

    def test_due_inside_its_window_and_missed_past_it(self):
        since = et(TUE, 0)
        assert checkins.standing(self.slot, et(TUE, 8, 59), since) is None
        assert checkins.standing(self.slot, et(TUE, 9), since) == "due"
        assert checkins.standing(self.slot, et(TUE, 13, 59), since) == "due"
        assert checkins.standing(self.slot, et(TUE, 14), since) == "missed"

    def test_a_window_that_closed_before_the_switch_was_on_is_never_missed(self):
        assert checkins.standing(self.slot, et(TUE, 15), active_since=et(TUE, 14, 30)) is None
        # Switched on mid-window: still due
        assert checkins.standing(self.slot, et(TUE, 12), active_since=et(TUE, 11)) == "due"


class TestClock:
    def test_the_fantasy_day_turns_at_three_in_the_morning_eastern(self):
        assert checkins.fantasy_day(et(TUE, 2, 59)) == MON
        assert checkins.fantasy_day(et(TUE, 3)) == TUE

    def test_a_manual_run_is_picked_by_the_clock_against_the_first_tip(self):
        tip = time(19)
        assert checkins.default_kind(et(TUE, 10), TUE, tip, MON) == "morning"
        assert checkins.default_kind(et(MON, 10), MON, tip, MON) == "weekly"
        assert checkins.default_kind(et(TUE, 17, 30), TUE, tip, MON) == "pre_lock"
        assert checkins.default_kind(et(TUE, 20), TUE, tip, MON) == "night"
        assert checkins.default_kind(et(TUE, 20), TUE, None, MON) == "morning"

    def test_the_night_follow_up_is_about_the_day_before(self):
        assert checkins.day_of("night", TUE) == MON
        assert checkins.day_of("pre_lock", TUE) == TUE


class TestBrief:
    def test_it_names_the_team_the_day_and_the_shadow_rules(self):
        text = checkins.brief("morning", TUE, 23, manual=False)
        assert text.startswith("[Morning check-in for team_id 23, about Tuesday 2026-11-10.")
        assert "It started on its schedule." in text
        assert "nothing you do reaches ESPN" in text and "log_decision" in text and "holds included" in text
        assert "get_automations" in text

    def test_each_kind_has_its_own_steps(self):
        assert "The user asked for it from the AI desk." in checkins.brief("pre_lock", TUE, 23, manual=True)
        assert "get_league" in checkins.brief("weekly", MON, 23, manual=False)
        night = checkins.brief("night", MON, 23, manual=False)
        assert "get_decisions" in night and "get_game_log" in night and "Monday 2026-11-09" in night
        assert "propose_lineup" in checkins.brief("pre_lock", TUE, 23, manual=False)

    def test_the_title_says_the_kind_and_the_day(self):
        assert checkins.title_of("night", MON) == "Night follow-up · Mon Nov 9"
        assert checkins.title_of("weekly", MON) == "Weekly check-in · Mon Nov 9"


# ------------------------------- the tick ------------------------------- #


@pytest.fixture
def tick(monkeypatch):
    """run_due over stubbed rows: one switched-on team (23, user 42) on a Tuesday with games."""
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "autopilot_enabled", True)
    monkeypatch.setattr(settings, "ai_agent_user_ids", [42])
    monkeypatch.setattr(settings, "autopilot_team_ids", [23])
    monkeypatch.setattr(settings, "autopilot_checkins_per_day", 6)
    # Switched on Monday night, after Monday's own check-ins had closed
    state = SimpleNamespace(rows=[SimpleNamespace(team_id_id=23, user_id_id=42, active_since=et(MON, 23),
                                                  paused_at=None)],
                            taken=set(), busy=False, ran=0, ran_scheduled_only=[], claims=[], started=[], swept=0,
                            broken=set())

    async def live():
        return state.rows

    async def tips(days):
        return {MON: time(19, 30), TUE: time(19)}

    async def taken(team_id, days):
        if team_id in state.broken:
            raise RuntimeError("the database hiccuped")
        return state.taken

    async def busy(team_id):
        return state.busy

    async def ran_since(team_id, since, *, scheduled_only=False):
        state.ran_scheduled_only.append(scheduled_only)
        return state.ran

    async def claim(team_id, user_id, kind, nba_date, due_at, *, manual, now, state="running", reason=None):
        state_.claims.append((kind, nba_date, state, reason))
        return len(state_.claims)

    state_ = state

    async def sweep(before, now):
        return state.swept

    async def start(team_id, user_id, kind, nba_date, due_at, *, manual, now):
        state.started.append((team_id, kind, nba_date, manual))
        return 900 + len(state.started)

    monkeypatch.setattr(checkins, "_live", live)
    monkeypatch.setattr(checkins, "_tips", tips)
    monkeypatch.setattr(checkins, "_taken", taken)
    monkeypatch.setattr(checkins, "_busy", busy)
    monkeypatch.setattr(checkins, "_ran_since", ran_since)
    monkeypatch.setattr(checkins, "_claim", claim)
    monkeypatch.setattr(checkins, "_sweep", sweep)
    monkeypatch.setattr(checkins, "_start", start)
    monkeypatch.setattr(checkins.schedule_service, "get_current_matchup",
                        lambda d: {"start_date": MON, "end_date": MON + timedelta(days=6)})
    return state


class TestTick:
    def test_it_starts_one_check_in_per_team_per_tick(self, tick):
        # 09:30: last night's follow-up and the morning are both due; the follow-up goes first
        out = run(checkins.run_due(et(TUE, 9, 30)))
        assert tick.started == [(23, "night", MON, False)]
        assert [(s.kind, s.nba_date) for s in out["started"]] == [("night", MON)]
        tick.taken = {("night", MON)}
        run(checkins.run_due(et(TUE, 9, 45)))
        assert tick.started[-1] == (23, "morning", TUE, False)

    def test_a_running_check_in_holds_the_next_one_back(self, tick):
        tick.busy = True
        out = run(checkins.run_due(et(TUE, 9, 30)))
        assert tick.started == [] and out["started"] == []

    def test_a_closed_window_is_recorded_as_missed(self, tick):
        tick.taken = {("night", MON)}
        out = run(checkins.run_due(et(TUE, 14, 30)))
        assert ("morning", TUE, "missed", "window_closed") in tick.claims and out["missed"] == 1
        assert tick.started == []

    def test_the_day_s_cap_skips_rather_than_runs(self, tick):
        tick.ran = settings.autopilot_checkins_per_day
        out = run(checkins.run_due(et(TUE, 9, 30)))
        assert tick.started == [] and out["skipped"] >= 1
        assert ("night", MON, "skipped", "daily_cap") in tick.claims

    def test_a_team_off_the_allowlist_or_a_kill_switch_runs_nothing(self, tick, monkeypatch):
        monkeypatch.setattr(settings, "autopilot_team_ids", [])
        assert run(checkins.run_due(et(TUE, 9, 30)))["teams"] == 0 and tick.started == []
        monkeypatch.setattr(settings, "autopilot_team_ids", [23])
        monkeypatch.setattr(settings, "autopilot_enabled", False)
        assert run(checkins.run_due(et(TUE, 9, 30)))["off"] is True and tick.started == []

    def test_the_day_s_cap_counts_scheduled_runs_only(self, tick):
        # Runs from the desk never use up a scheduled slot: the tick asks for scheduled ones alone
        run(checkins.run_due(et(TUE, 9, 30)))
        assert tick.ran_scheduled_only == [True]

    def test_one_team_s_failure_doesn_t_hold_back_the_next(self, tick, monkeypatch):
        monkeypatch.setattr(settings, "autopilot_team_ids", [23, 24])
        tick.rows.append(SimpleNamespace(team_id_id=24, user_id_id=42, active_since=et(MON, 23), paused_at=None))
        tick.broken = {23}
        out = run(checkins.run_due(et(TUE, 9, 30)))
        assert out["teams"] == 2 and tick.started == [(24, "night", MON, False)]

    def test_yesterday_s_check_ins_no_tick_reached_are_still_recorded_as_missed(self, tick):
        # The scheduler was down from Monday morning to past the rollover: Monday's weekly and
        # pre-lock are recorded missed on Tuesday's first tick, and Tuesday's follow-up still runs
        tick.rows[0].active_since = et(MON, 0)
        out = run(checkins.run_due(et(TUE, 9, 30)))
        assert ("weekly", MON, "missed", "window_closed") in tick.claims
        assert ("pre_lock", MON, "missed", "window_closed") in tick.claims
        assert out["missed"] == 2 and tick.started == [(23, "night", MON, False)]

    def test_switched_on_after_the_window_closed_is_not_missed(self, tick):
        tick.rows[0].active_since = et(TUE, 15)
        tick.taken = set()
        out = run(checkins.run_due(et(TUE, 15, 15)))
        assert out["missed"] == 0 and tick.claims == []


# ------------------------------- starting one ------------------------------- #


@pytest.fixture
def starting(monkeypatch):
    monkeypatch.setattr(settings, "autopilot_checkin_timeout_seconds", 60.0)
    state = SimpleNamespace(reservation=55, claim=7, cancelled=[], released=[], settled=[], claims=[],
                            created=[], launched=[], header_error=None, autopilot=None)

    async def autopilot_row(team_id):
        return state.autopilot

    async def reserve(user_id):
        return state.reservation

    async def cancel(reservation_id):
        state.cancelled.append(reservation_id)

    async def claim(team_id, user_id, kind, nba_date, due_at, *, manual, now, state="running", reason=None):
        state_.claims.append((kind, state, reason, manual))
        return state_.claim if state == "running" else None

    state_ = state

    async def create(user_id, team_id, *, kind="chat", title=None):
        state.created.append((user_id, team_id, kind, title))
        return SimpleNamespace(id=31)

    async def conv_claim(conversation_id, user_id, hold):
        return True

    async def release(conversation_id):
        state.released.append(conversation_id)

    async def attach(checkin_id, conversation_id):
        pass

    async def header(user_id, pinned, now=None, *, conversation_id=None):
        if state.header_error:
            raise state.header_error
        return f"[header {pinned} {conversation_id}]"

    async def settle(checkin_id, st, reason, turn_id, cost, now):
        state.settled.append((checkin_id, st, reason, turn_id, cost))

    async def fake_run(turn, header_line, checkin_id, team_id, kind):
        state.launched.append((turn, header_line, checkin_id))

    monkeypatch.setattr(spend, "reserve_autopilot", reserve)
    monkeypatch.setattr(spend, "cancel", cancel)
    monkeypatch.setattr(checkins, "_claim", claim)
    monkeypatch.setattr(conversations, "create", create)
    monkeypatch.setattr(conversations, "claim", conv_claim)
    monkeypatch.setattr(conversations, "release", release)
    monkeypatch.setattr(checkins, "_attach", attach)
    monkeypatch.setattr(agent, "header", header)
    monkeypatch.setattr(checkins, "_settle", settle)
    monkeypatch.setattr(checkins, "_run", fake_run)
    monkeypatch.setattr(checkins, "_autopilot_row", autopilot_row)
    return state


def _start(manual=False):
    async def go():
        out = await checkins._start(23, 42, "morning", TUE, et(TUE, 9), manual=manual, now=et(TUE, 9, 5))
        await asyncio.sleep(0)  # let the launched task run
        return out
    return run(go())


class TestStart:
    def test_it_opens_a_check_in_conversation_with_the_check_in_s_tools_and_bounds(self, starting):
        assert _start() == 7
        assert starting.created == [(42, 23, "checkin", "Morning check-in · Tue Nov 10")]
        turn, header_line, checkin_id = starting.launched[0]
        assert (checkin_id, header_line) == (7, "[header 23 31]")
        assert turn.tools is agent.CHECKIN_TOOLS and turn.allowed == agent.CHECKIN_ALLOWED
        assert turn.checkin_id == 7 and turn.reservation_id == 55 and turn.conversation_id == 31
        assert turn.limits == agent.Limits.checkin()
        assert turn.message.startswith("[Morning check-in for team_id 23")

    def test_acting_on_nothing_is_step_3_s_brief_and_rows_at_ai_say_so(self, starting, monkeypatch):
        _start()
        shadow = starting.launched[0][0].message
        assert shadow == checkins.brief("morning", TUE, 23, manual=False)
        assert checkins._SHADOW in shadow and "What you do yourself" not in shadow
        # At AI while acting is off on the server, it still reviews: step 3's brief
        starting.autopilot = SimpleNamespace(paused_at=None, ai_rows=["lineup"], autopilot=False)
        _start()
        assert starting.launched[1][0].message == shadow
        monkeypatch.setattr(settings, "dime_acting_enabled", True)
        _start()
        acting = starting.launched[2][0].message
        assert "What you do yourself on this team: lineup (slot moves and pins). Those automations are at AI" in acting
        assert checkins._SHADOW not in acting and "what you sent, what you left for them" in acting
        # The autopilot switch: every row that offers AI
        starting.autopilot = SimpleNamespace(paused_at=None, ai_rows=[], autopilot=True)
        _start()
        assert "lineup (slot moves and pins), daily actions" in starting.launched[3][0].message
        # A paused team only reviews, whatever is at AI
        starting.autopilot = SimpleNamespace(paused_at=datetime(2025, 11, 10, tzinfo=timezone.utc), ai_rows=["lineup"],
                                             autopilot=True)
        _start()
        assert starting.launched[4][0].message == shadow

    def test_a_spent_budget_skips_a_scheduled_one_and_refuses_a_manual_one(self, starting):
        starting.reservation = None
        assert _start() is None
        assert starting.claims == [("morning", "skipped", "budget", False)] and starting.created == []
        with pytest.raises(ServiceUnavailableError) as err:
            _start(manual=True)
        assert err.value.error_code == "AI_AUTOPILOT_BUDGET_REACHED"

    def test_a_slot_another_tick_took_hands_the_reservation_back(self, starting):
        starting.claim = None
        assert _start() is None
        assert starting.cancelled == [55] and starting.created == []

    def test_a_failure_before_the_turn_settles_it_failed_and_frees_everything(self, starting):
        starting.header_error = RuntimeError("teams unavailable")
        assert _start() == 7
        assert starting.cancelled == [55] and starting.released == [31]
        assert starting.settled == [(7, "failed", "start_failed", None, 0)] and starting.launched == []


class TestRun:
    def _run(self, monkeypatch, result):
        settled = []

        async def settle(checkin_id, st, reason, turn_id, cost, now):
            settled.append((checkin_id, st, reason, turn_id, cost))

        async def run_turn(turn, emit, *, header_line):
            return result
        monkeypatch.setattr(checkins, "_settle", settle)
        monkeypatch.setattr(agent, "run_turn", run_turn)
        turn = SimpleNamespace(cost=0, decisions=2, proposals=[1], user_id=42)
        run(checkins._run(turn, "[h]", 7, 23, "morning"))
        return settled

    def test_a_finished_turn_settles_it_done_with_its_cost(self, monkeypatch):
        assert self._run(monkeypatch, agent.TurnResult(turn_id=80, outcome="ok", cost=41_000)) == \
            [(7, "done", None, 80, 41_000)]

    def test_a_failed_turn_settles_it_failed_with_the_outcome(self, monkeypatch):
        assert self._run(monkeypatch, agent.TurnResult(turn_id=81, outcome="AI_TIMEOUT", cost=900_000)) == \
            [(7, "failed", "AI_TIMEOUT", 81, 900_000)]


class TestRunNow:
    @pytest.fixture
    def now_stubs(self, tick, monkeypatch):
        async def owns(team_id, user_id):
            return team_id == 23 and user_id == 42

        async def get(checkin_id):
            return SimpleNamespace(id=checkin_id)
        monkeypatch.setattr(checkins, "_owns", owns)
        monkeypatch.setattr(checkins, "_get", get)
        return tick

    def test_it_runs_off_the_schedule_picked_by_the_clock(self, now_stubs):
        row = run(checkins.run_now(42, 23, now=et(TUE, 18)))
        assert row.id == 901 and now_stubs.started == [(23, "pre_lock", TUE, True)]
        run(checkins.run_now(42, 23, "night", now=et(TUE, 10)))
        assert now_stubs.started[-1] == (23, "night", MON, True)

    def test_refused_off_the_allowlist_while_one_runs_and_past_the_cap(self, now_stubs, monkeypatch):
        with pytest.raises(AppError) as err:
            run(checkins.run_now(42, 99, now=et(TUE, 10)))
        assert err.value.error_code == "TEAM_NOT_FOUND"
        now_stubs.busy = True
        with pytest.raises(ConflictError):
            run(checkins.run_now(42, 23, now=et(TUE, 10)))
        now_stubs.busy, now_stubs.ran = False, settings.autopilot_checkins_per_day
        with pytest.raises(AppError) as err:
            run(checkins.run_now(42, 23, now=et(TUE, 10)))
        assert err.value.error_code == "AI_CHECKIN_DAILY_LIMIT"
        monkeypatch.setattr(settings, "autopilot_team_ids", [])
        with pytest.raises(AuthorizationError):
            run(checkins.run_now(42, 23, now=et(TUE, 10)))

    def test_a_run_from_the_desk_counts_every_check_in_toward_the_day_s_cap(self, now_stubs):
        run(checkins.run_now(42, 23, now=et(TUE, 10)))
        assert now_stubs.ran_scheduled_only == [False]

    def test_one_started_between_the_check_and_the_claim_is_refused_as_running(self, now_stubs, monkeypatch):
        async def lost(*args, **kwargs):
            return None
        monkeypatch.setattr(checkins, "_start", lost)
        with pytest.raises(ConflictError) as err:
            run(checkins.run_now(42, 23, now=et(TUE, 10)))
        assert err.value.error_code == "AI_CHECKIN_RUNNING"


class TestNextSlot:
    def test_the_next_one_still_open_today_or_tomorrow_s_first(self, monkeypatch):
        monkeypatch.setattr(checkins.schedule_service, "get_current_matchup",
                            lambda d: {"start_date": MON, "end_date": MON + timedelta(days=6)})
        cal = checkins.Calendar(today=TUE, tips={MON: time(19), TUE: time(19)}, matchup_start=MON)
        row = SimpleNamespace(paused_at=None, active_since=et(MON, 0))
        assert checkins.next_slot(cal, row, {("night", MON)}, et(TUE, 9, 30)).kind == "morning"
        assert checkins.next_slot(cal, row, {("night", MON), ("morning", TUE)}, et(TUE, 9, 30)).kind == "pre_lock"
        after = checkins.next_slot(cal, row, set(), et(TUE, 19))
        assert (after.kind, after.nba_date) == ("night", TUE)
        assert checkins.next_slot(cal, SimpleNamespace(paused_at=et(TUE, 1), active_since=et(MON, 0)), set(),
                                  et(TUE, 9)) is None


class TestTeamView:
    def test_the_switch_and_the_automations_table_with_lineup_from_the_preferences(self):
        prefs = SimpleNamespace(lineup_alerts_enabled=True, auto_lineup_enabled=True, alert_minutes_before=30)
        row = SimpleNamespace(paused_at=None, paused_by=None, paused_reason=None, active_since=et(MON, 0),
                              ai_rows=["daily_actions"], autopilot=False)
        view = checkins.team_view(23, row, None, prefs, None)
        assert view["on"] is True and view["autopilot"] is False and view["next"] is None
        assert {r["row"]: r["level"] for r in view["rows"]} == {
            "lineup": "act", "daily_actions": "ai", "lanes": "act", "late_scratch": "off", "checkins": "ai"}
        never = checkins.team_view(23, None, None, None, None)
        assert never["on"] is False and never["rows"][0]["level"] == "off"
        assert not next(o for o in never["rows"][0]["options"] if o["level"] == "ai")["available"]


# ------------------------------- decisions ------------------------------- #


def _args(**kw):
    base = {"team_id": 23, "area": "lineup", "automation": "auto_lineup", "call": "hold",
            "summary": "Keep auto-lineup's fill", "reason": "Every starter plays tonight"}
    return decisions.LogDecisionInput(**{**base, **kw})


class TestLogDecisionInput:
    def test_a_change_to_the_lineup_or_the_roster_cites_its_proposal(self):
        with pytest.raises(ValidationError, match="propose_lineup or propose_move first"):
            _args(call="change")
        with pytest.raises(ValidationError, match="propose_lineup or propose_move first"):
            _args(area="adds_drops", automation="none", call="change")
        assert _args(call="change", proposal_id=5).proposal_id == 5
        # A lane change has no tool yet: its summary says it
        assert _args(area="streaming", automation="stream_lane", call="change").proposal_id is None

    def test_a_hold_cites_nothing_and_words_are_squeezed(self):
        with pytest.raises(ValidationError, match="only a change cites a proposal"):
            _args(proposal_id=5)
        assert _args(summary="  Keep   the  fill ").summary == "Keep the fill"
        with pytest.raises(ValidationError):
            _args(summary="ok")


@pytest.fixture
def logging_stubs(monkeypatch):
    state = SimpleNamespace(checkin=SimpleNamespace(id=7, state="running", team_id_id=23, conversation_id_id=31),
                            count=0, proposal=None, inserted=[])

    async def checkin(checkin_id, user_id):
        return state.checkin if checkin_id == 7 and user_id == 42 else None

    async def count(checkin_id):
        return state.count

    async def proposal(proposal_id, conversation_id):
        return state.proposal if conversation_id == 31 else None

    async def insert(**fields):
        state.inserted.append(fields)
        return SimpleNamespace(id=60, proposal_id_id=fields.get("proposal_id"), **{k: v for k, v in fields.items()
                                                                                  if k != "proposal_id"},
                               verdict=None)
    monkeypatch.setattr(decisions, "_checkin", checkin)
    monkeypatch.setattr(decisions, "_count", count)
    monkeypatch.setattr(decisions, "_proposal", proposal)
    monkeypatch.setattr(decisions, "_insert", insert)
    return state


class TestLogDecision:
    def test_it_logs_the_call_and_hands_the_desk_its_block(self, logging_stubs):
        out = run(decisions.log_decision(42, 7, _args()))
        assert out["logged"] is True and out["decision_id"] == 60 and out["move"] is None
        block = out[decisions.DECISION_KEY]
        assert block == {"decision_id": 60, "area": "lineup", "automation": "auto_lineup", "call": "hold",
                         "summary": "Keep auto-lineup's fill", "reason": "Every starter plays tonight",
                         "proposal_id": None, "verdict": None, "action": None}
        assert "action" not in out          # a hold commits nothing
        assert logging_stubs.inserted[0]["team_id"] == 23 and logging_stubs.inserted[0]["checkin_id"] == 7

    def test_only_a_running_check_in_on_its_own_team_logs(self, logging_stubs):
        with pytest.raises(AppError, match="Only a check-in logs decisions"):
            run(decisions.log_decision(42, None, _args()))
        with pytest.raises(AppError, match="team_id 23"):
            run(decisions.log_decision(42, 7, _args(team_id=24)))
        logging_stubs.count = decisions.PER_CHECKIN
        with pytest.raises(AppError, match="as many decisions"):
            run(decisions.log_decision(42, 7, _args()))
        logging_stubs.count, logging_stubs.checkin.state = 0, "done"
        with pytest.raises(AppError, match="no longer running"):
            run(decisions.log_decision(42, 7, _args()))
        assert logging_stubs.inserted == []

    def test_a_change_cites_a_proposal_made_in_this_check_in(self, logging_stubs):
        with pytest.raises(AppError, match="wasn't made in this check-in"):
            run(decisions.log_decision(42, 7, _args(call="change", proposal_id=5)))
        assert logging_stubs.inserted == []

    def test_a_proposal_for_another_of_the_user_s_teams_isn_t_cited(self, logging_stubs):
        logging_stubs.proposal = SimpleNamespace(id=5, team_id_id=24)
        with pytest.raises(AppError, match="for another team"):
            run(decisions.log_decision(42, 7, _args(area="adds_drops", automation="none", call="change",
                                                    proposal_id=5)))
        assert logging_stubs.inserted == []


class TestRefreshHistory:
    def test_decision_blocks_carry_the_verdict_as_it_stands(self, monkeypatch):
        row = SimpleNamespace(id=60, area="lineup", automation="auto_lineup", call="hold", summary="Keep it",
                              reason="All play", proposal_id_id=None, verdict="agree")

        async def of_conversation(conversation_id, user_id):
            return [row]

        async def actions_of(decision_ids):
            assert decision_ids == [60]
            return {60: {"outcome": "sent", "rule": None, "message": "Sent by Dime", "at": None}}
        monkeypatch.setattr(decisions, "_of_conversation", of_conversation)
        monkeypatch.setattr(decisions.acting, "actions_of", actions_of)
        part = {"kind": "block", "block": {"type": "decision", "id": "d1", "decision_id": 60, "verdict": None}}
        text = {"kind": "text", "text": "hi"}
        out = run(decisions.refresh_history([[text, part]], 31, 42))
        assert out[0][0] == text
        assert out[0][1]["block"]["verdict"] == "agree" and out[0][1]["block"]["id"] == "d1"
        assert out[0][1]["block"]["type"] == "decision"
        assert out[0][1]["block"]["action"]["outcome"] == "sent"      # what Dime did about it, as it stands

    def test_a_chat_without_decisions_reads_nothing(self, monkeypatch):
        async def boom(*a):
            raise AssertionError("no read")
        monkeypatch.setattr(decisions, "_of_conversation", boom)
        history = [[{"kind": "text", "text": "hi"}]]
        assert run(decisions.refresh_history(history, 31, 42)) is history


class TestVerdict:
    def test_only_agree_or_disagree(self):
        with pytest.raises(AppError):
            run(decisions.set_verdict(60, 42, "maybe"))


# ------------------------------- shadow proposals ------------------------------- #


class TestShadowProposals:
    def test_a_shadow_chip_says_it_was_logged_not_sent(self):
        words = proposals.summary_of("transaction", "Herb Jones", "Kris Dunn", TUE, shadow=True)
        assert words == "Add Herb Jones, drop Kris Dunn, today. Logged in shadow, not sent."
        moves = [{"name": "Herb Jones", "role": "start", "to_slot": "SF"}]
        assert proposals.lineup_summary(moves, TUE, today=False, shadow=True).endswith(
            "Tue 11/10. Logged in shadow, not sent.")

    def test_a_shadow_chip_is_never_stageable(self):
        row = SimpleNamespace(state="shadow", expires_at=et(TUE, 19))
        assert proposals.state_of(row, et(TUE, 20)) == "shadow"
        assert "can't be sent" in proposals._NOT_PENDING["shadow"]


# ------------------------------- the automations ------------------------------- #


class TestFillSetting:
    def test_without_account_alerts_the_fill_never_runs(self):
        assert automations.fill_setting(None, None) == automations.FillSetting(False, False, 0)
        prefs = SimpleNamespace(lineup_alerts_enabled=False, auto_lineup_enabled=True, alert_minutes_before=90)
        team = SimpleNamespace(lineup_alerts_enabled=True, auto_lineup_enabled=True, alert_minutes_before=60)
        # The pipeline skips the user before it reads the team override
        assert automations.fill_setting(prefs, team).runs is False

    def test_a_team_override_wins_where_it_is_set(self):
        prefs = SimpleNamespace(lineup_alerts_enabled=True, auto_lineup_enabled=False, alert_minutes_before=90)
        assert automations.fill_setting(prefs, None) == automations.FillSetting(True, False, 90)
        team = SimpleNamespace(lineup_alerts_enabled=None, auto_lineup_enabled=True, alert_minutes_before=None)
        assert automations.fill_setting(prefs, team) == automations.FillSetting(True, True, 90)
        off = SimpleNamespace(lineup_alerts_enabled=False, auto_lineup_enabled=True, alert_minutes_before=None)
        assert automations.fill_setting(prefs, off) == automations.FillSetting(False, False, 90)


def test_the_check_in_window_math_matches_et_at():
    # The slots are built from the same ET-to-UTC helper the scheduled pickups use
    assert et(TUE, 9) == et_at(TUE, time(9))
