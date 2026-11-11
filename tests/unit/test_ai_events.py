"""
Dime's event check-ins (the Dime autopilot plan, step 6): the lines an event reads as, the
brief that carries them, when a kick starts a check-in for them (or leaves them to the
scheduled one about to run), what a post from data-platform records, and a check-in that
takes them. The DB is stubbed here; the rows, the unique indexes and the reads run against
the real schema in tests/integration/test_dime_events.py.
"""

import asyncio
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest

from core.nba_calendar import EASTERN
from core.settings import settings
from services.ai import agent, checkins, conversations, events, spend

pytestmark = [pytest.mark.unit]

TUE = date(2026, 11, 10)
MON = date(2026, 11, 9)


def et(d, hh, mm=0):
    return EASTERN.localize(datetime.combine(d, time(hh, mm))).astimezone(timezone.utc)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def on(monkeypatch):
    monkeypatch.setattr(settings, "ai_enabled", True)
    monkeypatch.setattr(settings, "autopilot_enabled", True)
    monkeypatch.setattr(settings, "ai_agent_user_ids", [42])
    monkeypatch.setattr(settings, "autopilot_team_ids", [23])
    monkeypatch.setattr(settings, "autopilot_checkins_per_day", 6)
    monkeypatch.setattr(settings, "autopilot_event_checkins_per_day", 3)
    monkeypatch.setattr(settings, "autopilot_event_fold_minutes", 20)


# ------------------------------- the lines ------------------------------- #


class TestSummaries:
    def test_a_verdict_reads_as_who_what_which_game_who_said_and_what_it_was(self):
        line = events.verdict_summary("Jalen Brunson", "out", "questionable", "espn_fantasy", "Left Knee", TUE,
                                      "BOS @ NYK", et(TUE, 19, 30), et(TUE, 18, 12))
        assert line == ("Jalen Brunson ruled out for Tue Nov 10, BOS @ NYK (7:30 PM ET tip), "
                        "per ESPN fantasy at 6:12 PM ET; was questionable. Left Knee")

    def test_a_verdict_with_less_to_say_says_less(self):
        line = events.verdict_summary("Josh Hart", "available", None, None, None, TUE, None, None, None)
        assert line == "Josh Hart cleared to play for Tue Nov 10"

    def test_a_news_item_reads_as_its_headline_with_its_source_and_time(self):
        assert events.news_summary("Vit Krejci", "Vit Krejci: Suffers leg fracture", "rotowire_rss",
                                   et(TUE, 0, 17)) == "Vit Krejci: Suffers leg fracture (Rotowire, 12:17 AM ET)"
        assert events.news_summary("Kon Knueppel", "Leaves Game With Leg Soreness", "realgm_rss", None) == \
            "Kon Knueppel: Leaves Game With Leg Soreness (RealGM)"

    def test_a_long_headline_is_cut_to_fit(self):
        line = events.news_summary("A B", "x" * 600, "espn_player_news", None)
        assert len(line) == 400 and line.endswith("… (ESPN)")

    def test_the_brief_lines_carry_the_player_id_the_tools_take(self):
        taken = [SimpleNamespace(summary="Josh Hart cleared to play for Tue Nov 10", player_id=1628404)]
        assert events.brief_lines(taken) == ["- Josh Hart cleared to play for Tue Nov 10 (player_id 1628404)"]


class TestBrief:
    LINES = ["- Josh Hart cleared to play for Tue Nov 10 (player_id 1628404)"]

    def test_an_event_check_in_opens_with_what_came_in_then_its_steps(self):
        text = checkins.brief(checkins.EVENT, TUE, 23, manual=False, events=self.LINES)
        assert text.startswith("[Event check-in for team_id 23, about Tuesday 2026-11-10. "
                               "It started because news came in about the roster.")
        assert "\n\nWhat came in:\n- Josh Hart cleared to play" in text
        assert text.index("What came in:") < text.index(checkins._STEPS[checkins.EVENT])
        assert "get_player_news" in checkins._STEPS[checkins.EVENT]

    def test_a_scheduled_check_in_weighs_what_came_in_since_the_last_one(self):
        text = checkins.brief("pre_lock", TUE, 23, manual=False, events=self.LINES)
        assert "What came in since your last check-in" in text and "player_id 1628404" in text
        assert checkins.brief("pre_lock", TUE, 23, manual=False) == checkins.brief("pre_lock", TUE, 23,
                                                                                    manual=False, events=())

    def test_an_event_check_in_acts_only_where_late_scratches_do(self):
        live = checkins.brief(checkins.EVENT, TUE, 23, manual=False, acting=["late_scratch"], events=self.LINES)
        assert "Late scratches are at AI on this team and this is their check-in" in live
        assert "what you sent" in live
        # Lineup at AI doesn't make an event check-in's lineup move go: it counts under late scratches
        shadow = checkins.brief(checkins.EVENT, TUE, 23, manual=False, acting=["lineup"], events=self.LINES)
        assert shadow == checkins.brief(checkins.EVENT, TUE, 23, manual=False, events=self.LINES)
        assert "You run it in shadow" in shadow

    def test_a_scheduled_check_in_never_acts_as_late_scratches(self):
        both = checkins.brief("pre_lock", TUE, 23, manual=False, acting=["lineup", "late_scratch"])
        assert both == checkins.brief("pre_lock", TUE, 23, manual=False, acting=["lineup"])
        assert "late scratches" not in both
        assert checkins.brief("pre_lock", TUE, 23, manual=False, acting=["late_scratch"]) == \
            checkins.brief("pre_lock", TUE, 23, manual=False)

    def test_an_event_check_in_can_t_be_asked_for_from_the_desk(self):
        assert checkins.EVENT not in checkins.KINDS
        assert checkins.title_of(checkins.EVENT, TUE) == "Event check-in · Tue Nov 10"


# ------------------------------- the kick ------------------------------- #


def _event(tip=None):
    return SimpleNamespace(tip_at=tip, summary="x", player_id=1)


@pytest.fixture
def kicking(on, monkeypatch):
    """kick over stubbed rows: team 23 (user 42) on a Tuesday with a 7 PM first tip, switched on Monday,
    with its Late scratches row at AI."""
    state = SimpleNamespace(row=SimpleNamespace(team_id_id=23, user_id_id=42, active_since=et(MON, 0),
                                                paused_at=None, ai_rows=["late_scratch"], autopilot=False),
                            waiting=[_event()], busy=False, runs=[], taken={("night", MON)}, event_checkins=0,
                            dropped=[], slots_started=[], events_started=[])

    async def autopilot_row(team_id):
        return state.row

    async def drop_stale(team_id, now):
        state.dropped.append(now)
        return 0

    async def pending(team_id):
        return list(state.waiting)

    async def busy(team_id):
        return state.busy

    async def tips(days):
        return {MON: time(19, 30), TUE: time(19)}

    async def taken(team_id, days):
        return {(kind, day) for kind, day in state.taken if day in days}

    async def run_times(team_id, since):
        return [at for at in state.runs if at >= since]

    async def event_checkins_on(team_id, nba_date):
        return state.event_checkins

    async def start_slot(row, slot, now):
        state.slots_started.append((slot.kind, slot.nba_date))
        return checkins.Started(71, row.team_id_id, slot.kind, slot.nba_date)

    async def start_event(row, today, now):
        state.events_started.append(today)
        return checkins.Started(72, row.team_id_id, checkins.EVENT, today)

    monkeypatch.setattr(checkins, "_autopilot_row", autopilot_row)
    monkeypatch.setattr(events, "drop_stale", drop_stale)
    monkeypatch.setattr(events, "pending", pending)
    monkeypatch.setattr(checkins, "_busy", busy)
    monkeypatch.setattr(checkins, "_tips", tips)
    monkeypatch.setattr(checkins, "_taken", taken)
    monkeypatch.setattr(checkins, "_run_times", run_times)
    monkeypatch.setattr(events, "_event_checkins_on", event_checkins_on)
    monkeypatch.setattr(checkins, "start_slot", start_slot)
    monkeypatch.setattr(checkins, "start_event", start_event)
    monkeypatch.setattr(checkins.schedule_service, "get_current_matchup",
                        lambda d: {"start_date": MON, "end_date": MON + timedelta(days=6)})
    return state


def kick(at):
    return run(events.kick(23, 42, at))


class TestKick:
    def test_between_check_ins_an_event_check_in_starts(self, kicking):
        # 15:00: the morning is taken; pre-lock opens at 18:15, hours away
        kicking.taken |= {("morning", TUE)}
        started = kick(et(TUE, 15))
        assert started.kind == checkins.EVENT and kicking.events_started == [TUE]
        assert kicking.dropped, "stale events are dropped before anything is counted"

    def test_a_scheduled_check_in_that_is_due_takes_them(self, kicking):
        # 09:30: the morning is due and not yet run (the tick hasn't reached it)
        started = kick(et(TUE, 9, 30))
        assert (started.kind, kicking.slots_started) == ("morning", [("morning", TUE)])
        assert kicking.events_started == []

    def test_one_about_to_open_is_waited_for(self, kicking):
        kicking.taken |= {("morning", TUE)}
        assert kick(et(TUE, 18)) is None                       # pre-lock opens at 18:15
        assert kicking.events_started == [] and kicking.slots_started == []

    def test_unless_a_game_tips_before_it_could_answer(self, kicking):
        kicking.taken |= {("morning", TUE)}
        kicking.waiting = [_event(tip=et(TUE, 18, 30))]       # tips 15 minutes after pre-lock opens
        assert kick(et(TUE, 18)).kind == checkins.EVENT

    def test_nothing_pending_or_a_check_in_running_starts_nothing(self, kicking):
        kicking.taken |= {("morning", TUE)}
        kicking.busy = True
        assert kick(et(TUE, 15)) is None
        kicking.busy, kicking.waiting = False, []
        assert kick(et(TUE, 15)) is None
        assert kicking.events_started == []

    def test_a_night_follow_up_that_ran_is_not_started_again(self, kicking):
        # 10:00: Monday's night follow-up (it runs on Tuesday's morning, about Monday) and the morning ran
        kicking.taken |= {("morning", TUE)}
        started = kick(et(TUE, 10))
        assert started.kind == checkins.EVENT and kicking.slots_started == []

    def test_an_event_check_in_never_takes_a_scheduled_one_s_place_under_the_day_s_cap(self, kicking):
        kicking.taken |= {("morning", TUE)}
        # Five ran in the day: one more, and pre-lock at 18:15 would be the seventh
        kicking.runs = [et(MON, 18, 45), et(TUE, 6), et(TUE, 9), et(TUE, 11), et(TUE, 12)]
        assert kick(et(TUE, 15)) is None
        kicking.runs = kicking.runs[:-1]
        assert kick(et(TUE, 15)) is not None

    def test_nor_tomorrow_morning_s_the_cap_counts_a_rolling_day(self, kicking):
        # 21:00, after pre-lock: today's three ran, then two event check-ins. A third would make six
        # in the day before tomorrow's 06:00 night follow-up, and the tick would skip it at the cap
        kicking.taken |= {("morning", TUE), ("pre_lock", TUE)}
        kicking.runs = [et(TUE, 6, 15), et(TUE, 9, 15), et(TUE, 18, 15), et(TUE, 19), et(TUE, 20)]
        assert kick(et(TUE, 21)) is None
        kicking.runs = kicking.runs[:-1]
        assert kick(et(TUE, 21)) is not None

    def test_event_check_ins_have_their_own_daily_cap(self, kicking):
        kicking.taken |= {("morning", TUE)}
        kicking.event_checkins = 3
        assert kick(et(TUE, 15)) is None

    def test_off_paused_or_off_the_allowlist_starts_nothing(self, kicking, monkeypatch):
        kicking.taken |= {("morning", TUE)}
        kicking.row.paused_at = et(TUE, 8)
        assert kick(et(TUE, 15)) is None
        kicking.row.paused_at = None
        monkeypatch.setattr(settings, "autopilot_team_ids", [])
        assert kick(et(TUE, 15)) is None
        monkeypatch.setattr(settings, "autopilot_team_ids", [23])
        monkeypatch.setattr(settings, "autopilot_enabled", False)
        assert kick(et(TUE, 15)) is None
        assert kicking.events_started == []

    def test_only_on_a_team_whose_late_scratches_row_is_at_ai(self, kicking):
        kicking.taken |= {("morning", TUE)}
        # Another row at AI isn't enough: events are Late scratches' AI
        kicking.row.ai_rows = ["lineup", "daily_actions"]
        assert kick(et(TUE, 15)) is None and kicking.events_started == []
        # The autopilot switch puts it there
        kicking.row.autopilot = True
        assert kick(et(TUE, 15)).kind == checkins.EVENT


# ------------------------------- receiving ------------------------------- #


@pytest.fixture
def receiving(on, monkeypatch):
    rows = [SimpleNamespace(team_id_id=23, user_id_id=42, paused_at=None, active_since=et(MON, 0),
                            ai_rows=["late_scratch"], autopilot=False),
            SimpleNamespace(team_id_id=24, user_id_id=42, paused_at=None, active_since=et(MON, 0),
                            ai_rows=[], autopilot=True)]
    monkeypatch.setattr(settings, "autopilot_team_ids", [23, 24])
    found = [events.Fact(source=events.AVAILABILITY, player_id=1628973, summary="Brunson ruled out",
                         game_date=TUE, verdict="out", changed_at=et(TUE, 18)),
             events.Fact(source=events.NEWS, player_id=1628404, summary="Hart: news", news_item_id=9)]
    state = SimpleNamespace(rows=rows, found=found, rosters={23: frozenset({1628973}), 24: frozenset({7})},
                            recorded=[], kicked=[], asked=[], new=1)

    async def live():
        return state.rows

    async def facts(availability, news, now):
        state.asked.append((availability, news))
        return state.found

    async def roster_ids(team_id, user_id):
        return state.rosters.get(team_id)

    async def record(team_id, user_id, nba_date, mine, now):
        state.recorded.append((team_id, nba_date, [f.player_id for f in mine]))
        return state.new

    async def kick(team_id, user_id, now, *, row=None):
        state.kicked.append(team_id)
        return checkins.Started(80, team_id, checkins.EVENT, TUE)

    monkeypatch.setattr(checkins, "_live", live)
    monkeypatch.setattr(events, "facts", facts)
    monkeypatch.setattr(events, "roster_ids", roster_ids)
    monkeypatch.setattr(events, "record", record)
    monkeypatch.setattr(events, "kick", kick)
    return state


class TestReceive:
    def test_each_fact_is_an_event_on_the_teams_that_roster_the_player(self, receiving):
        out = run(events.receive([(1628973, TUE)], [9], et(TUE, 18, 5)))
        assert receiving.asked == [([(1628973, TUE)], [9])]
        assert receiving.recorded == [(23, TUE, [1628973])]       # team 24 rosters neither
        assert receiving.kicked == [23]
        assert (out["teams"], out["facts"], out["recorded"]) == (2, 2, 1)
        assert [s.checkin_id for s in out["started"]] == [80]

    def test_an_event_already_recorded_kicks_no_one(self, receiving):
        receiving.new = 0
        out = run(events.receive([(1628973, TUE)], [], et(TUE, 18, 5)))
        assert receiving.kicked == [] and out["recorded"] == 0

    def test_a_roster_espn_won_t_serve_records_nothing_for_that_team(self, receiving):
        receiving.rosters = {}
        out = run(events.receive([(1628973, TUE)], [9], et(TUE, 18, 5)))
        assert receiving.recorded == [] and out["recorded"] == 0

    def test_off_nothing_is_read_or_recorded(self, receiving, monkeypatch):
        monkeypatch.setattr(settings, "autopilot_enabled", False)
        out = run(events.receive([(1628973, TUE)], [9], et(TUE, 18, 5)))
        assert out["off"] is True and receiving.asked == [] and receiving.recorded == []

    def test_an_empty_post_reads_nothing(self, receiving):
        out = run(events.receive([], [], et(TUE, 18, 5)))
        assert receiving.asked == [] and out["teams"] == 2

    def test_a_team_below_ai_on_late_scratches_hears_nothing(self, receiving):
        receiving.rows[0].ai_rows = ["lineup"]                     # team 23: lineup at AI, late scratches not
        out = run(events.receive([(1628973, TUE)], [9], et(TUE, 18, 5)))
        assert out["teams"] == 1 and receiving.recorded == [] and receiving.kicked == []
        receiving.rows[1].autopilot = False                        # team 24 too: no one listens, nothing is read
        out = run(events.receive([(1628973, TUE)], [9], et(TUE, 18, 5)))
        assert out["teams"] == 0 and receiving.asked == [([(1628973, TUE)], [9])]


# ------------------------------- the roster read ------------------------------- #


class TestRoster:
    def test_a_read_is_kept_for_a_while_and_the_last_one_serves_when_espn_fails(self, monkeypatch):
        from services.ai import acting, proposals
        monkeypatch.setattr(events, "_ROSTERS", {})
        calls, down = [], []

        async def team(team_id, user_id):
            return SimpleNamespace(team_id=team_id), {}

        async def board(team, league_info):
            calls.append(team.team_id)
            if down:
                raise RuntimeError("ESPN down")
            return SimpleNamespace(players=[SimpleNamespace(nba_player_id=1628973),
                                            SimpleNamespace(nba_player_id=None)])

        monkeypatch.setattr(acting, "_team", team)
        monkeypatch.setattr(proposals, "_board", board)
        monkeypatch.setattr(settings, "autopilot_event_roster_ttl_seconds", 600)
        assert run(events.roster_ids(23, 42)) == frozenset({1628973})
        assert run(events.roster_ids(23, 42)) == frozenset({1628973}) and calls == [23]
        # Expired, and ESPN fails: the last read serves
        monkeypatch.setattr(events, "_ROSTERS", {23: (0.0, frozenset({5}))})
        down.append(True)
        assert run(events.roster_ids(23, 42)) == frozenset({5})
        # Never read, and ESPN fails: no roster
        events.forget_roster(23)
        assert run(events.roster_ids(23, 42)) is None


# ------------------------------- a check-in takes them ------------------------------- #


@pytest.fixture
def starting(on, monkeypatch):
    monkeypatch.setattr(settings, "autopilot_checkin_timeout_seconds", 60.0)
    state = SimpleNamespace(taken=[], released=[], cancelled=[], settled=[], launched=[], header_error=None,
                            take_rows=[], autopilot=None)

    async def reserve(user_id):
        return 55

    async def cancel(reservation_id):
        state.cancelled.append(reservation_id)

    async def claim(team_id, user_id, kind, nba_date, due_at, *, manual, now, state="running", reason=None):
        return 7

    async def take(team_id, checkin_id, now=None, *, row):
        state.take_rows.append(row)
        return list(state.taken)

    async def release(checkin_id):
        state.released.append(checkin_id)
        return len(state.taken)

    async def create(user_id, team_id, *, kind="chat", title=None):
        return SimpleNamespace(id=31)

    async def conv_claim(conversation_id, user_id, hold):
        return True

    async def conv_release(conversation_id):
        pass

    async def attach(checkin_id, conversation_id):
        pass

    async def header(user_id, pinned, now=None, *, conversation_id=None):
        if state.header_error:
            raise state.header_error
        return "[header]"

    async def settle(checkin_id, st, reason, turn_id, cost, now):
        state.settled.append((checkin_id, st, reason))

    async def autopilot_row(team_id):
        return state.autopilot

    async def fake_run(turn, header_line, checkin_id, team_id, kind):
        state.launched.append(turn)

    monkeypatch.setattr(spend, "reserve_autopilot", reserve)
    monkeypatch.setattr(spend, "cancel", cancel)
    monkeypatch.setattr(checkins, "_claim", claim)
    monkeypatch.setattr(events, "take", take)
    monkeypatch.setattr(events, "release", release)
    monkeypatch.setattr(conversations, "create", create)
    monkeypatch.setattr(conversations, "claim", conv_claim)
    monkeypatch.setattr(conversations, "release", conv_release)
    monkeypatch.setattr(checkins, "_attach", attach)
    monkeypatch.setattr(agent, "header", header)
    monkeypatch.setattr(checkins, "_settle", settle)
    monkeypatch.setattr(checkins, "_autopilot_row", autopilot_row)
    monkeypatch.setattr(checkins, "_run", fake_run)
    return state


def _start(kind):
    async def go():
        out = await checkins._start(23, 42, kind, TUE, et(TUE, 15), manual=False, now=et(TUE, 15))
        await asyncio.sleep(0)
        return out
    return run(go())


EVENT_ROW = SimpleNamespace(summary="Josh Hart cleared to play for Tue Nov 10", player_id=1628404)


class TestTaking:
    def test_a_check_in_s_brief_lists_the_events_it_took(self, starting):
        starting.taken = [EVENT_ROW]
        starting.autopilot = SimpleNamespace(ai_rows=["late_scratch"], autopilot=False, paused_at=None)
        assert _start("pre_lock") == 7
        message = starting.launched[0].message
        assert "What came in since your last check-in" in message and "(player_id 1628404)" in message
        assert starting.take_rows == [starting.autopilot]          # what it takes is the team's levels' call

    def test_an_event_check_in_with_nothing_left_to_take_is_skipped(self, starting):
        assert _start(checkins.EVENT) is None
        assert starting.settled == [(7, "skipped", "no_events")] and starting.cancelled == [55]
        assert starting.launched == []

    def test_a_check_in_that_fails_to_start_gives_its_events_back(self, starting):
        starting.taken = [EVENT_ROW]
        starting.header_error = RuntimeError("teams unavailable")
        assert _start(checkins.EVENT) == 7
        assert starting.released == [7] and starting.settled == [(7, "failed", "start_failed")]

    def test_a_finished_check_in_kicks_the_team_again(self, on, monkeypatch):
        kicked, forgot = [], []

        async def fake_kick(team_id, user_id, now, *, row=None):
            kicked.append((team_id, user_id))

        monkeypatch.setattr(events, "kick", fake_kick)
        monkeypatch.setattr(events, "forget_roster", forgot.append)
        run(checkins._after(23, 42))
        assert kicked == [(23, 42)] and forgot == [23]
        monkeypatch.setattr(settings, "autopilot_enabled", False)
        run(checkins._after(23, 42))
        assert kicked == [(23, 42)]


@pytest.mark.parametrize("ai, autopilot", [(False, True), (True, False)])
def test_with_a_kill_switch_off_no_check_in_takes_an_event(monkeypatch, ai, autopilot):
    monkeypatch.setattr(settings, "ai_enabled", ai)
    monkeypatch.setattr(settings, "autopilot_enabled", autopilot)
    assert not events.enabled()
    assert run(events.take(23, 7, row=SimpleNamespace(ai_rows=["late_scratch"], autopilot=False))) == []


def test_events_have_no_switch_of_their_own(on):
    assert events.enabled()


def test_below_ai_a_check_in_drops_what_was_waiting_instead_of_taking_it(on, monkeypatch):
    dropped, took = [], []

    async def drop_all(team_id, now, reason):
        dropped.append((team_id, reason))
        return 2

    async def take_(team_id, checkin_id, now):
        took.append(checkin_id)
        return []

    monkeypatch.setattr(events, "_drop_all", drop_all)
    monkeypatch.setattr(events, "_take", take_)
    assert run(events.take(23, 7, et(TUE, 15), row=SimpleNamespace(ai_rows=["lineup"], autopilot=False))) == []
    assert dropped == [(23, "level")] and took == []
    assert run(events.take(23, 7, et(TUE, 15), row=None)) == [] and dropped[-1] == (23, "level")
