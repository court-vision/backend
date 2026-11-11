"""
services/ai/autonomy.py, services/ai/levels.py and services/ai/acting.py: a team's automation
levels, and Dime acting on its own (the Dime autopilot plan, step 5), without Postgres, ESPN or
the model.

Covers the levels (a row at AI by the user or by the autopilot switch, acting only with check-ins
on and DIME_ACTING_ENABLED on), which rows a chip counts under, the automations table (every
level's availability and why not, the notes), a settings change's checks, the commitment (a
review does nothing; at AI the move is held to the policy and sent through the user's own
routes, once), every rule of the policy, auto-pause and its email, and the decision a check-in
logs carrying what became of its move.
"""

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from core.errors import AppError, ProviderAuthError
from core.settings import settings
from schemas.lineup_editor import AcquisitionState
from services.ai import acting, autonomy, decisions, levels, proposals
from services.lineup_editor_service import RosterWriteRejected
from tests.unit.test_ai_proposals import (
    BE, EDWARDS, JOKIC, KAWHI, LEAGUE, NBA_EDWARDS, NBA_JOKIC, NOW, TEAM, TODAY, WEEK, player, state,
)
from tests.unit.test_ai_proposals import row as _row

pytestmark = pytest.mark.unit

TOMORROW = date(2026, 10, 22)


def run(coro):
    return asyncio.run(coro)


def prop(**kw):
    """A usr.ai_proposals row (step 4's `action` included, and the team as peewee's `team_id_id` too)."""
    kw.setdefault("action", None)
    kw.setdefault("team_id_id", kw.get("team_id", 21))
    return _row(**kw)


def pilot(**kw):
    """A usr.autopilot row (check-ins on, nothing at AI)."""
    base = dict(team_id=21, user_id=11, ai_rows=[], autopilot=False, restore=None,
                paused_at=None, paused_by=None, paused_reason=None)
    base.update(kw)
    return SimpleNamespace(**base)


def chip(kind, **kw):
    """A proposal row of a kind, with only what the mapping reads."""
    return SimpleNamespace(kind=kind, action=kw.pop("action", None), add_player_id=kw.pop("add_player_id", None), **kw)


# ------------------------------- the levels ------------------------------- #


def prefs(alerts=True, auto=False):
    """The account's notification preferences (lineup's levels below AI)."""
    return SimpleNamespace(lineup_alerts_enabled=alerts, auto_lineup_enabled=auto, alert_minutes_before=30)


def team_pref(alerts=None, auto=None):
    """A team's override (None inherits the account's)."""
    return SimpleNamespace(lineup_alerts_enabled=alerts, auto_lineup_enabled=auto, alert_minutes_before=None)


class TestLevels:
    def test_a_row_is_at_ai_when_the_user_put_it_there_or_the_switch_is_on(self, monkeypatch):
        monkeypatch.setattr(settings, "dime_acting_enabled", True)
        assert autonomy.ai_rows(None) == [] and not autonomy.at_ai(None, autonomy.LINEUP)
        team = pilot(ai_rows=["daily_actions", "bogus"])
        assert autonomy.stored(team) == {"daily_actions"}                     # an unknown row is ignored
        assert autonomy.ai_rows(team) == ["daily_actions"]
        on = pilot(autopilot=True, ai_rows=["daily_actions"])
        # The switch puts every row that offers AI at AI, late scratches too (step 6's event check-ins)
        assert autonomy.ai_rows(on) == ["lineup", "daily_actions", "lanes", "late_scratch"]
        assert autonomy.at_ai(on, autonomy.LATE_SCRATCH) and not autonomy.offers_ai(autonomy.CHECKINS)
        assert autonomy.ai_rows(pilot(ai_rows=["late_scratch"])) == ["late_scratch"]

    def test_dime_acts_only_with_check_ins_on_and_acting_on_on_the_server(self, monkeypatch):
        monkeypatch.setattr(settings, "dime_acting_enabled", True)
        team = pilot(ai_rows=["lineup", "lanes"])
        assert autonomy.acting_rows(team) == ["lineup", "lanes"]
        assert autonomy.acts(team, autonomy.LINEUP) and not autonomy.acts(team, autonomy.DAILY_ACTIONS)
        paused = pilot(ai_rows=["lineup"], paused_at=NOW)
        assert autonomy.acting_rows(paused) == [] and autonomy.ai_rows(paused) == ["lineup"]
        monkeypatch.setattr(settings, "dime_acting_enabled", False)
        assert autonomy.acting_rows(team) == []                              # every change is a review

    def test_the_brief_s_words(self):
        assert autonomy.words(["lineup", "lanes"]) == (
            "lineup (slot moves and pins), stream lanes (starting, pausing, stopping and steering them)")


class TestWhatAChipTouches:
    def test_each_kind_of_chip_counts_under_one_or_more_rows(self):
        rows = autonomy.rows_of
        assert rows(chip("lineup")) == {"lineup"}
        assert rows(chip("transaction")) == rows(chip("scheduled")) == {"daily_actions"}
        assert rows(chip("lane", action={"op": "start"})) == {"lanes"}
        assert rows(chip("cancel_pickup", action={"pickup_id": 4, "lane_id": 9})) == {"lanes"}
        assert rows(chip("cancel_pickup", action={"pickup_id": 4, "lane_id": None})) == {"daily_actions"}
        assert rows(chip("steer", action={"holds": [{"kind": "keep"}, {"kind": "block"}]})) == {"lanes"}
        assert rows(chip("steer", action={"holds": [{"kind": "pin"}], "clear": [{"kind": "keep"}]})) == \
            {"lineup", "lanes"}
        # An override at 0 is read as out by the fill too
        assert rows(chip("steer", action={"holds": [{"kind": "override", "share": 0}]})) == {"lineup", "lanes"}
        assert rows(chip("steer", action={"holds": [{"kind": "override", "share": 0.5}]})) == {"lanes"}
        # Whatever an event check-in commits counts under late scratches
        assert rows(chip("transaction"), event=True) == {"late_scratch"}

    def test_a_chip_acts_only_if_every_row_it_touches_does(self, monkeypatch):
        monkeypatch.setattr(settings, "dime_acting_enabled", True)
        team = pilot(ai_rows=["lanes"])
        both = chip("steer", action={"holds": [{"kind": "pin"}, {"kind": "keep"}]})
        assert not autonomy.chip_acts(team, both)
        assert autonomy.chip_acts(team, chip("lane", action={"op": "stop"}))
        assert autonomy.chip_acts(pilot(ai_rows=["lanes", "lineup"]), both)
        # An event check-in's move acts only where late scratches do, whatever its kind's row says
        assert not autonomy.chip_acts(pilot(ai_rows=["lineup"]), chip("lineup"), event=True)
        assert autonomy.chip_acts(pilot(ai_rows=["late_scratch"]), chip("lineup"), event=True)
        assert autonomy.chip_acts(pilot(autopilot=True), chip("transaction"), event=True)
        assert autonomy.is_event("event") and not autonomy.is_event("morning")
        assert autonomy.primary({"lanes", "lineup"}) == "lineup"

    def test_only_an_add_counts_toward_the_caps(self):
        assert autonomy.adds_of(chip("transaction", add_player_id=KAWHI)) == 1
        assert autonomy.adds_of(chip("scheduled", add_player_id=KAWHI)) == 1
        assert autonomy.adds_of(chip("transaction", add_player_id=None)) == 0     # a drop alone
        assert autonomy.adds_of(chip("lane", add_player_id=KAWHI)) == 0          # the lane's own budget


def by_row(table):
    return {r["row"]: r for r in table}


def offered(row):
    return {o["level"]: o["reason"] for o in row["options"] if not o["available"]}


class TestTheTable:
    def test_every_row_says_which_levels_it_offers_and_why_not(self, monkeypatch):
        monkeypatch.setattr(settings, "dime_acting_enabled", True)
        t = by_row(autonomy.table(None, lineup_below_ai="notify", lineup_email=True))
        assert list(t) == ["lineup", "daily_actions", "lanes", "late_scratch", "checkins"]
        assert [o["level"] for o in t["lineup"]["options"]] == ["off", "notify", "act", "ai"]
        assert offered(t["lineup"]) == {}
        assert set(offered(t["daily_actions"])) == {"notify", "act"}
        assert set(offered(t["lanes"])) == {"off", "notify"}
        assert offered(t["late_scratch"]) == {}
        assert offered(t["checkins"]) == {"notify": "Check-ins are Dime's own", "act": "Check-ins are Dime's own"}
        # Never set up: lineup and late scratches from the preferences, lanes run as set, the rest off
        assert {r: t[r]["level"] for r in t} == {"lineup": "notify", "daily_actions": "off", "lanes": "act",
                                                 "late_scratch": "off", "checkins": "off"}
        t = by_row(autonomy.table(None, lineup_below_ai="act", lineup_email=False, late_scratch_below_ai="act"))
        assert t["late_scratch"]["level"] == "act"
        assert offered(t["late_scratch"]) == {}     # its own email: the account's lineup email doesn't gate it
        at_ai = by_row(autonomy.table(pilot(ai_rows=["late_scratch"]), lineup_below_ai="act", lineup_email=True,
                                      late_scratch_below_ai="notify"))
        assert at_ai["late_scratch"]["level"] == "ai"

    def test_the_levels_and_notes_of_a_team_at_ai(self, monkeypatch):
        monkeypatch.setattr(settings, "dime_acting_enabled", True)
        monkeypatch.setattr(settings, "dime_events_enabled", False)
        t = by_row(autonomy.table(pilot(ai_rows=["daily_actions"]), lineup_below_ai="act", lineup_email=True))
        assert (t["daily_actions"]["level"], t["daily_actions"]["note"]) == ("ai", None)
        assert (t["lineup"]["level"], t["checkins"]["level"]) == ("act", "ai")
        on = by_row(autonomy.table(pilot(autopilot=True), lineup_below_ai="act", lineup_email=True))
        assert [on[r]["level"] for r in on] == ["ai", "ai", "ai", "ai", "ai"]
        assert on["lineup"]["note"] == "Autopilot sets these"
        # Late scratches at AI do nothing while the server's event check-ins are off
        assert on["late_scratch"]["note"] == "Nothing starts yet: event check-ins are switched off on the server"
        monkeypatch.setattr(settings, "dime_events_enabled", True)
        on = by_row(autonomy.table(pilot(autopilot=True), lineup_below_ai="act", lineup_email=True))
        assert on["late_scratch"]["note"] == "Autopilot sets these"
        monkeypatch.setattr(settings, "dime_acting_enabled", False)
        off = by_row(autonomy.table(pilot(ai_rows=["lanes"]), lineup_below_ai="act", lineup_email=True))
        assert off["lanes"]["note"] == "Reviews only: Dime's acting is switched off on the server"

    def test_paused_and_a_pause_dime_made_itself(self, monkeypatch):
        monkeypatch.setattr(settings, "dime_acting_enabled", True)
        mine = by_row(autonomy.table(pilot(ai_rows=["lanes"], paused_at=NOW, paused_by="user"),
                                     lineup_below_ai="off", lineup_email=True))
        assert mine["lanes"]["note"].startswith("Paused") and mine["checkins"]["level"] == "off"
        assert mine["checkins"]["note"] is None
        its = by_row(autonomy.table(pilot(paused_at=NOW, paused_by="system", paused_reason="2 refused today"),
                                    lineup_below_ai="off", lineup_email=True))
        assert its["checkins"]["note"] == "Dime paused itself: 2 refused today"

    def test_the_account_s_lineup_email_off_closes_every_lineup_level_but_off(self, monkeypatch):
        monkeypatch.setattr(settings, "dime_acting_enabled", True)
        t = by_row(autonomy.table(pilot(ai_rows=["lineup"]), lineup_below_ai="off", lineup_email=False))
        assert set(offered(t["lineup"])) == {"notify", "act", "ai"}
        assert "lineup email is off" in offered(t["lineup"])["ai"]
        assert t["lineup"]["note"] == "Auto-lineup doesn't run: your account's lineup email is off"


class TestLineupBelowAi:
    def test_lineup_s_level_is_the_tip_off_email_and_auto_lineup(self):
        assert levels.lineup_below_ai(None, None) == "off"                       # no preferences row: nothing runs
        assert levels.lineup_below_ai(prefs(alerts=False, auto=True), team_pref(alerts=True)) == "off"
        assert levels.lineup_below_ai(prefs(), None) == "notify"
        assert levels.lineup_below_ai(prefs(auto=True), None) == "act"
        assert levels.lineup_below_ai(prefs(auto=True), team_pref(auto=False)) == "notify"   # the team's wins
        assert levels.lineup_below_ai(prefs(auto=True), team_pref(alerts=False)) == "off"

    def test_each_level_writes_the_team_s_override(self):
        assert levels.lineup_prefs_for("off") == {"lineup_alerts_enabled": False, "auto_lineup_enabled": False}
        assert levels.lineup_prefs_for("notify") == {"lineup_alerts_enabled": True, "auto_lineup_enabled": False}
        assert levels.lineup_prefs_for("act") == levels.lineup_prefs_for("ai") == \
            {"lineup_alerts_enabled": True, "auto_lineup_enabled": True}
        for level in ("off", "notify", "act"):
            assert levels.lineup_below_ai(prefs(), team_pref(**dict(zip(
                ("alerts", "auto"), levels.lineup_prefs_for(level).values())))) == level


class TestTheCheck:
    def test_check_ins_in_levels_is_the_check_ins_switch(self):
        assert levels.check(levels.Change(levels={"checkins": "ai", "lanes": "ai"}), email=True) == \
            levels.Change(checkins=True, levels={"lanes": "ai"})
        assert levels.check(levels.Change(levels={"checkins": "off"}), email=True).checkins is False
        assert levels.check(levels.Change(checkins=True), email=True) == levels.Change(checkins=True)
        assert levels.check(levels.Change(autopilot=True), email=False).autopilot is True

    @pytest.mark.parametrize("change, words", [
        (levels.Change(), "Nothing to change"),
        (levels.Change(autopilot=True, levels={"lanes": "ai"}), "not both"),
        (levels.Change(levels={"trades": "ai"}), "Unknown automation"),
        (levels.Change(levels={"lanes": "max"}), "off, notify, act or ai"),
        (levels.Change(checkins=False, levels={"checkins": "ai"}), "disagree"),
    ])
    def test_a_change_that_makes_no_sense_is_refused(self, change, words):
        with pytest.raises(AppError, match=words) as e:
            levels.check(change, email=True)
        assert e.value.status_code == 422

    def test_a_level_a_row_doesn_t_offer_is_refused_with_why(self):
        assert levels.check(levels.Change(levels={"late_scratch": "act"}), email=False).levels == {"late_scratch": "act"}
        assert levels.check(levels.Change(levels={"late_scratch": "ai"}), email=True).levels == {"late_scratch": "ai"}
        with pytest.raises(levels.LevelUnavailable, match="Daily actions can't be notify: Not built yet"):
            levels.check(levels.Change(levels={"daily_actions": "notify"}), email=True)
        with pytest.raises(levels.LevelUnavailable, match="lineup email is off"):
            levels.check(levels.Change(levels={"lineup": "act"}), email=False)
        assert levels.check(levels.Change(levels={"lineup": "off"}), email=False).levels == {"lineup": "off"}
        assert levels.LevelUnavailable().status_code == 409


# ------------------------------- the commitment ------------------------------- #


def roster(*, edwards_value=20.0, edwards_status=None):
    """Jokic and Edwards; Edwards (the drop) the lesser."""
    jokic = player(JOKIC, "Nikola Jokic", nba_id=NBA_JOKIC)
    edwards = player(EDWARDS, "Anthony Edwards", BE, team="MIN", nba_id=NBA_EDWARDS).model_copy(
        update={"avg_points": edwards_value, "injury_status": edwards_status})
    return state([jokic, edwards])


CHECKIN = SimpleNamespace(id=9, team_id_id=21)


@pytest.fixture
def h(monkeypatch):
    """Every switch on, team 21's daily actions at AI, one shadow add/drop for today; the DB, the board and
    the send faked."""
    for k, v in dict(dime_acting_enabled=True, ai_enabled=True, autopilot_enabled=True, roster_writes_enabled=True,
                     dime_steering_enabled=True, autopilot_team_ids=[21], ai_agent_user_ids=[11],
                     autopilot_adds_per_day=2, autopilot_adds_per_matchup=5, autopilot_adds_reserve=1,
                     autopilot_protected_top=1, autopilot_churn_hours=48, autopilot_pause_refused=2,
                     autopilot_pause_same_block=2, frontend_url="https://courtvision.dev/").items():
        monkeypatch.setattr(settings, k, v)
    h = SimpleNamespace(rows={31: prop(state="shadow", summary="... Logged in shadow, not sent.")},
                        autopilot=pilot(ai_rows=["daily_actions"]), actions={}, recorded=[], promoted=[], paused=[], emails=[],
                        recent=set(), board=roster(), sends=[], raises=None, earlier=[])

    async def proposal(pid):
        return h.rows.get(pid)

    async def autopilot_row(team_id):
        return h.autopilot

    async def promote(pid, summary, now):
        r = h.rows[pid]
        if r.state != "shadow" or r.expires_at <= now:
            return False
        h.rows[pid] = prop(**{**vars(r), "state": "proposed", "summary": summary})
        h.promoted.append(summary)
        return True

    async def action_of(pid):
        return h.actions.get(pid)

    async def record(**fields):
        if fields["proposal_id"] in h.actions:
            return None
        a = SimpleNamespace(id=70 + len(h.recorded), **fields)
        h.actions[fields["proposal_id"]] = a
        h.recorded.append(fields)
        return a

    async def adds_sent(team_id, start, end):
        done = [*h.earlier, *h.recorded]
        return sum(f["adds"] for f in done if f["outcome"] == "sent" and start <= f["nba_date"] <= end)

    async def count_since(team_id, since, outcome, rule=None):
        return sum(1 for f in h.recorded if f["outcome"] == outcome and (rule is None or f["rule"] == rule))

    async def recent_adds(team_id, player_id, since, since_day):
        return player_id in h.recent

    async def team(team_id, user_id):
        return (TEAM, LEAGUE) if (team_id, user_id) == (21, 11) else (None, None)

    async def pause(team_id, reason, now):
        if h.autopilot.paused_at is not None:
            return False
        h.autopilot.paused_at, h.autopilot.paused_by = now, "system"
        h.paused.append(reason)
        return True

    async def recipient(user_id):
        return "owner@example.com"

    async def board(team, league_info, scoring_period_id=None):
        return h.board

    async def send(team, league_info, r):
        h.sends.append(r.id)
        await asyncio.sleep(0)            # let anything running alongside go first
        if h.raises is not None:
            raise h.raises
        return "Added Kawhi Leonard, dropped Anthony Edwards", 88

    def email(self, to, subject, body):
        h.emails.append((to, subject, body))

    monkeypatch.setattr(acting, "_proposal", proposal)
    monkeypatch.setattr(acting, "_autopilot_row", autopilot_row)
    monkeypatch.setattr(acting, "_promote", promote)
    monkeypatch.setattr(acting, "_action_of", action_of)
    monkeypatch.setattr(acting, "_record", record)
    monkeypatch.setattr(acting, "_adds_sent", adds_sent)
    monkeypatch.setattr(acting, "_count_since", count_since)
    monkeypatch.setattr(acting, "_recent_adds", recent_adds)
    monkeypatch.setattr(acting, "_team", team)
    monkeypatch.setattr(acting, "_pause", pause)
    monkeypatch.setattr(acting, "_recipient", recipient)
    monkeypatch.setattr(acting, "_send", send)
    monkeypatch.setattr(proposals, "_board", board)
    monkeypatch.setattr(acting.schedule_service, "get_nba_today", lambda: TODAY)
    monkeypatch.setattr(acting.schedule_service, "get_current_matchup", lambda today: dict(WEEK))
    from services.notification_service import NotificationService
    monkeypatch.setattr(NotificationService, "_send_email", email)
    return h


def commit(pid=31, decision_id=60):
    return run(acting.commit(11, CHECKIN, decision_id, pid, now=NOW))


def sent(nba_date, adds=1):
    return {"outcome": "sent", "nba_date": nba_date, "adds": adds, "rule": None}


class TestCommit:
    def test_below_ai_it_is_a_review_and_nothing_happens(self, h):
        h.autopilot = pilot(ai_rows=["lineup"])       # the lineup at AI is not this move's row
        v = commit()
        assert v == acting.Verdict("review")
        assert h.rows[31].state == "shadow" and h.promoted == [] and h.sends == [] and h.recorded == []

    def test_with_acting_off_on_the_server_or_check_ins_paused_it_is_a_review(self, h, monkeypatch):
        h.autopilot = pilot(autopilot=True, paused_at=NOW)
        assert commit() == acting.Verdict("review")
        h.autopilot = pilot(autopilot=True)
        monkeypatch.setattr(settings, "dime_acting_enabled", False)
        assert commit() == acting.Verdict("review")
        assert h.rows[31].state == "shadow" and h.sends == []

    def test_under_the_autopilot_switch_the_move_is_sent(self, h):
        h.autopilot = pilot(autopilot=True)
        v = commit()
        assert v.level == "ai" and v.outcome == "sent" and h.sends == [31]

    def test_at_ai_the_move_is_sent_and_recorded_once(self, h):
        v = commit()
        assert v.level == "ai" and v.outcome == "sent" and v.audit_id == 88 and v.action_id == 70
        assert v.message == "Sent by Dime: Added Kawhi Leonard, dropped Anthony Edwards"
        assert v.view() == {"level": "ai", "outcome": "sent", "message": v.message}
        assert h.promoted == ["Add Kawhi Leonard, drop Anthony Edwards, today. Goes to ESPN when you send it."]
        assert h.sends == [31]
        rec = h.recorded[0]
        assert (rec["team_id"], rec["user_id"], rec["checkin_id"], rec["decision_id"], rec["proposal_id"]) == \
            (21, 11, 9, 60, 31)
        assert (rec["capability"], rec["kind"], rec["nba_date"], rec["adds"], rec["audit_id"]) == \
            ("daily_actions", "transaction", TODAY, 1, 88)
        # A second decision citing the same move never sends it again
        again = commit(decision_id=61)
        assert again.outcome == "sent" and again.message.startswith("Already handled: Sent by Dime")
        assert h.sends == [31] and len(h.recorded) == 1

    def test_a_move_whose_day_has_started_stays_logged(self, h):
        h.rows[31] = prop(state="shadow", expires_at=NOW - timedelta(minutes=1))
        v = commit()
        assert v.level == "review" and "stays logged" in v.message and h.sends == []

    def test_a_proposal_of_another_team_or_a_team_no_longer_the_user_s_does_nothing(self, h):
        h.rows[31] = prop(state="shadow", team_id=24)
        assert commit() == acting.Verdict("review") and h.promoted == []
        h.rows[31] = prop(state="shadow")
        v = run(acting.commit(12, CHECKIN, 60, 31, now=NOW))
        assert v.level == "review" and "no longer the user's" in v.message and h.sends == []

    def test_a_chip_the_user_already_sent_or_dismissed_is_left_alone(self, h):
        h.rows[31] = prop(state="dismissed")
        v = commit()
        assert v.level == "ai" and "dismissed" in v.message and h.sends == [] and h.recorded == []

    def test_two_decisions_in_one_response_never_both_pass_the_day_s_cap(self, h, monkeypatch):
        monkeypatch.setattr(settings, "autopilot_adds_per_day", 1)
        h.rows[32] = prop(id=32, state="shadow", add_player_id=99, add_name="Herb Jones")

        async def both():
            return await asyncio.gather(acting.commit(11, CHECKIN, 60, 31, now=NOW),
                                        acting.commit(11, CHECKIN, 61, 32, now=NOW))
        first, second = run(both())
        assert first.outcome == "sent" and second.outcome == "blocked" and second.rule == "cap_day"
        assert h.sends == [31]


class TestPolicy:
    def test_the_day_s_cap_counts_dime_s_own_adds_on_the_move_s_day(self, h):
        h.earlier = [sent(TODAY), sent(TODAY), sent(TOMORROW)]
        v = commit()
        assert (v.outcome, v.rule) == ("blocked", "cap_day")
        assert v.message == "Dime has made its 2 add(s) for Wed Oct 21; left as a chip for the user"
        assert h.sends == [] and h.recorded[0]["outcome"] == "blocked" and h.rows[31].state == "proposed"

    def test_the_matchup_s_cap_counts_the_whole_matchup(self, h):
        h.earlier = [sent(date(2026, 10, 19), 2), sent(date(2026, 10, 20), 2), sent(TOMORROW)]
        assert commit().rule == "cap_matchup"

    def test_the_last_of_espn_s_adds_this_matchup_is_kept_for_the_user(self, h):
        h.board = roster().model_copy(update={"acquisitions": AcquisitionState(
            per="matchup", limit=7, matchup_start=WEEK["start_date"], matchup_end=WEEK["end_date"], matchup_used=6)})
        v = commit()
        assert v.rule == "reserve" and v.message.startswith("1 of the league's adds left this matchup")
        h.board = roster().model_copy(update={"acquisitions": AcquisitionState(
            per="matchup", limit=7, matchup_start=WEEK["start_date"], matchup_end=WEEK["end_date"], matchup_used=5)})
        h.rows[32] = prop(id=32, state="shadow")
        assert commit(32).outcome == "sent"

    def test_one_of_the_roster_s_top_players_is_never_dropped(self, h, monkeypatch):
        monkeypatch.setattr(settings, "autopilot_protected_top", 2)
        v = commit()
        assert v.rule == "protected" and v.message.startswith("Anthony Edwards is one of the roster's top 2")

    def test_a_player_added_recently_is_not_dropped_unless_he_is_out(self, h):
        h.recent = {EDWARDS}
        v = commit()
        assert v.rule == "churn" and "in the last 48 hours" in v.message
        h.board = roster(edwards_status="OUT")
        h.rows[32] = prop(id=32, state="shadow")
        assert commit(32).outcome == "sent"

    def test_a_switch_that_is_off_blocks_and_never_pauses_the_team(self, h, monkeypatch):
        monkeypatch.setattr(settings, "roster_writes_enabled", False)
        for pid in (31, 32, 33):
            h.rows[pid] = prop(id=pid, state="shadow")
            v = commit(pid, decision_id=60 + pid)
            assert (v.outcome, v.rule, v.paused) == ("blocked", "writes_off", None)
        assert h.paused == [] and h.sends == []

    def test_a_steering_chip_waits_for_steering_and_writes_nothing(self, h, monkeypatch):
        steer = prop(state="shadow", kind="steer", add_player_id=None, drop_player_id=None,
                    action={"holds": [{"kind": "keep", "player": {"name": "Nikola Jokic"}, "starts_on": "2026-10-21",
                                       "ends_on": "2026-10-22"}], "clear": []})
        h.rows[31] = steer
        h.autopilot = pilot(ai_rows=["lanes"])                            # a keep hold counts under lanes
        monkeypatch.setattr(settings, "roster_writes_enabled", False)     # not an ESPN write: no matter
        monkeypatch.setattr(settings, "dime_steering_enabled", False)
        assert commit().rule == "steering_off"
        monkeypatch.setattr(settings, "dime_steering_enabled", True)
        h.rows[32] = prop(**{**vars(steer), "id": 32, "state": "shadow"})
        v = commit(32)
        assert v.outcome == "sent" and h.recorded[-1]["capability"] == "lanes" and h.recorded[-1]["adds"] == 0

    def test_a_paused_or_closed_team_and_a_server_switch_stop_it(self, h, monkeypatch):
        monkeypatch.setattr(settings, "autopilot_team_ids", [23])
        assert commit().rule == "not_allowed"
        monkeypatch.setattr(settings, "autopilot_team_ids", [21])
        monkeypatch.setattr(settings, "autopilot_enabled", False)
        h.rows[32] = prop(id=32, state="shadow")
        assert commit(32).rule == "autopilot_off"


class TestAutoPause:
    def test_two_refused_writes_in_a_day_pause_the_team_and_email_the_user(self, h):
        h.raises = RosterWriteRejected("ESPN rejected the lineup change", message="Player is on waivers")
        first = commit()
        assert (first.outcome, first.rule, first.paused) == ("refused", "espn_rejected", None)
        assert first.message == "ESPN refused it: Player is on waivers; left as a chip for the user"
        h.rows[32] = prop(id=32, state="shadow")
        second = commit(32)
        assert second.paused == "2 of its moves were refused today" and second.view()["paused"] == second.paused
        assert h.autopilot.paused_by == "system"
        (to, subject, body), = h.emails
        assert to == "owner@example.com" and subject == "Dime paused itself on GloatingSoap369"
        assert "https://courtvision.dev/account?v=automation" in body and "ESPN refused it" in body
        # Paused, the next change stays logged
        h.rows[33] = prop(id=33, state="shadow")
        assert commit(33) == acting.Verdict("review") and h.sends == [31, 32]

    def test_a_credential_error_pauses_at_once(self, h):
        h.raises = ProviderAuthError("espn")
        v = commit()
        assert (v.outcome, v.rule) == ("failed", "credentials") and v.paused == "ESPN refused the team's credentials"
        assert len(h.emails) == 1

    def test_the_same_rule_stopping_it_twice_pauses_it(self, h):
        h.earlier = [sent(TODAY), sent(TODAY)]
        assert commit().paused is None
        h.rows[32] = prop(id=32, state="shadow")
        v = commit(32)
        assert v.rule == "cap_day" and v.paused.startswith("the policy stopped the same kind of move 2 times today")

    def test_a_failed_email_never_undoes_the_pause(self, h, monkeypatch):
        from services.notification_service import NotificationService

        def boom(self, to, subject, body):
            raise RuntimeError("smtp down")
        monkeypatch.setattr(NotificationService, "_send_email", boom)
        h.raises = ProviderAuthError("espn")
        assert commit().paused is not None and h.autopilot.paused_at is not None


class TestClassify:
    def test_what_a_route_raises_is_an_outcome(self):
        from services.lineup_editor_service import RosterWriteDisabled
        assert acting._classify(ProviderAuthError("espn"))[:2] == ("failed", "credentials")
        assert acting._classify(RosterWriteDisabled())[:2] == ("blocked", "writes_off")
        assert acting._classify(AppError("AI_STEERING_OFF", "Steering is off", status_code=409))[:2] == \
            ("blocked", "steering_off")
        assert acting._classify(AppError("ROSTER_STALE", "The roster changed", status_code=409)) == \
            ("refused", "ROSTER_STALE", "The roster changed")
        assert acting._classify(AppError("PROVIDER_UNAVAILABLE", "ESPN is down", status_code=502))[:2] == \
            ("failed", "PROVIDER_UNAVAILABLE")
        assert acting._classify(ValueError("x")) == ("failed", "error", "Something went wrong sending it")


# ------------------------------- the send ------------------------------- #


@pytest.fixture
def routes(monkeypatch):
    """The routes' services, recording what they were handed."""
    from services.ai import steering as ai_steering
    from services.roster_transaction_service import RosterTransactionService
    from services.scheduled_pickup_service import ScheduledPickupService
    from services.stream_lane_service import StreamLaneService
    calls = []

    async def board(team, league_info, scoring_period_id=None):
        return roster()

    async def apply(team, league_info, req, *, source="manual"):
        calls.append(("apply", req, source))
        return SimpleNamespace(message="Added Kawhi Leonard", data=SimpleNamespace(audit_id=88))

    async def schedule(team, league_info, req, *, by_autopilot=False):
        calls.append(("schedule", req, by_autopilot))
        return SimpleNamespace(message="Scheduled", data=None)

    async def holds(team, proposal_id, now=None, *, by="user"):
        calls.append(("holds", proposal_id, by))
        return 1

    async def stop(team, lane_id, proposal_id=None, *, by="user"):
        calls.append(("stop", lane_id, proposal_id, by))
        return SimpleNamespace(message="Lane stopped")

    monkeypatch.setattr(proposals, "_board", board)
    monkeypatch.setattr(RosterTransactionService, "apply", staticmethod(apply))
    monkeypatch.setattr(ScheduledPickupService, "schedule", staticmethod(schedule))
    monkeypatch.setattr(ai_steering, "apply_holds", holds)
    monkeypatch.setattr(StreamLaneService, "stop", staticmethod(stop))
    return calls


class TestSend:
    def test_a_transaction_goes_through_the_user_s_route_on_the_fresh_board_as_dime_s(self, routes):
        message, audit_id = run(acting._send(TEAM, LEAGUE, prop(state="proposed")))
        assert (message, audit_id) == ("Added Kawhi Leonard", 88)
        (_, req, source), = routes
        assert (req.add_player_id, req.drop_player_id, req.proposal_id, req.roster_version) == (KAWHI, EDWARDS, 31, "v1")
        assert source == "autopilot"

    def test_a_scheduled_pickup_is_marked_as_dime_s(self, routes):
        run(acting._send(TEAM, LEAGUE, prop(state="proposed", kind="scheduled", nba_date=TOMORROW, scoring_period_id=3)))
        (_, req, by_autopilot), = routes
        assert req.scoring_period_id == 3 and req.proposal_id == 31 and by_autopilot is True

    def test_steering_chips_are_applied_as_dime(self, routes):
        msg, _ = run(acting._send(TEAM, LEAGUE, prop(kind="steer", action={"holds": [{}], "clear": [{}, {}]})))
        assert msg == "Set 1 hold(s), cleared 2" and routes[-1] == ("holds", 31, "dime")
        run(acting._send(TEAM, LEAGUE, prop(kind="lane", action={"op": "stop", "lane_id": 5})))
        assert routes[-1] == ("stop", 5, 31, "dime")

    def test_a_lineup_already_set_on_espn_is_sent_and_still_pins_its_bench_as_dime_s(self, routes, monkeypatch):
        from services import steering
        done = []

        async def mark_sent(proposal_id, audit_id=None):
            done.append(("sent", proposal_id))

        async def pin(proposal_id, now=None, *, by="user"):
            done.append(("pinned", proposal_id, by))

        monkeypatch.setattr(proposals, "live_moves", lambda board, row: [])
        monkeypatch.setattr(proposals, "mark_sent", mark_sent)
        monkeypatch.setattr(steering, "pin_benched", pin)
        msg, audit_id = run(acting._send(TEAM, LEAGUE, prop(kind="lineup", scoring_period_id=1)))
        assert (msg, audit_id) == ("The lineup was already set this way on ESPN", None)
        assert done == [("sent", 31), ("pinned", 31, "dime")]
        assert not routes                                             # nothing went to ESPN


class TestWords:
    def test_a_live_chip_says_what_it_says_outside_a_check_in(self, monkeypatch):
        monkeypatch.setattr(acting.schedule_service, "get_nba_today", lambda: TODAY)
        assert acting.live_summary(prop(state="shadow")) == \
            "Add Kawhi Leonard, drop Anthony Edwards, today. Goes to ESPN when you send it."
        assert "Logged in shadow" not in acting.live_summary(prop(kind="scheduled", nba_date=TOMORROW))

    def test_the_pause_email_says_why_what_it_tried_and_where_to_resume(self, monkeypatch):
        monkeypatch.setattr(settings, "frontend_url", "https://courtvision.dev")
        subject, body = acting.pause_email("Soap", "2 of its moves were refused today", "ESPN refused it")
        assert subject == "Dime paused itself on Soap"
        assert body.startswith("Dime stopped acting on Soap: 2 of its moves were refused today.")
        assert "The last move it tried: ESPN refused it" in body
        assert "https://courtvision.dev/account?v=automation" in body


# ------------------------------- the decision ------------------------------- #


class TestTheDecisionCarriesTheAction:
    @pytest.fixture
    def logging(self, monkeypatch):
        h = SimpleNamespace(checkin=SimpleNamespace(id=7, state="running", team_id_id=21, conversation_id_id=7),
                            proposal=prop(state="shadow"), commits=[], verdict=acting.Verdict("review"))

        async def checkin(checkin_id, user_id):
            return h.checkin

        async def count(checkin_id):
            return 0

        async def proposal(proposal_id, conversation_id):
            return h.proposal

        async def insert(**fields):
            return SimpleNamespace(id=60, proposal_id_id=fields.get("proposal_id"), verdict=None,
                                   **{k: v for k, v in fields.items() if k != "proposal_id"})

        async def commit_(user_id, checkin, decision_id, proposal_id):
            h.commits.append((user_id, checkin.id, decision_id, proposal_id))
            if h.verdict.level != "review":
                h.proposal = prop(state="sent" if h.verdict.outcome == "sent" else "proposed")
            return h.verdict
        monkeypatch.setattr(decisions, "_now", lambda: NOW)
        monkeypatch.setattr(decisions, "_checkin", checkin)
        monkeypatch.setattr(decisions, "_count", count)
        monkeypatch.setattr(decisions, "_proposal", proposal)
        monkeypatch.setattr(decisions, "_insert", insert)
        monkeypatch.setattr(decisions.acting, "commit", commit_)
        return h

    def args(self):
        return decisions.LogDecisionInput(team_id=21, area="adds_drops", automation="none", call="change",
                                          summary="Add Kawhi for Edwards", reason="4 games left", proposal_id=31)

    def test_a_review_is_step_3_s_decision(self, logging):
        out = run(decisions.log_decision(11, 7, self.args()))
        assert logging.commits == [(11, 7, 60, 31)]
        assert "action" not in out and out[decisions.DECISION_KEY]["action"] is None
        assert out["move"]["state"] == "shadow"

    def test_a_move_dime_sent_is_in_the_result_and_on_the_block(self, logging):
        logging.verdict = acting.Verdict("ai", "sent", None, "Sent by Dime: Added Kawhi Leonard", 88, 70)
        out = run(decisions.log_decision(11, 7, self.args()))
        assert out["action"] == {"level": "ai", "outcome": "sent", "message": "Sent by Dime: Added Kawhi Leonard"}
        block = out[decisions.DECISION_KEY]["action"]
        assert block["outcome"] == "sent" and block["message"].startswith("Sent by Dime") and block["at"]
        assert out["move"]["state"] == "sent"                  # the chip re-read after the send

    def test_a_chip_dime_left_alone_has_no_outcome(self, logging):
        logging.verdict = acting.Verdict("ai", message="The chip is dismissed, so Dime left it")
        out = run(decisions.log_decision(11, 7, self.args()))
        assert out["action"]["level"] == "ai" and out["action"]["outcome"] is None
        assert out[decisions.DECISION_KEY]["action"] is None


class TestLateScratchBelowAi:
    """Late scratches' level below AI: the team's override, else the account's, else off."""

    def test_the_override_wins_then_the_account(self):
        account = SimpleNamespace(late_scratch_level="notify")
        assert levels.late_scratch_below_ai(None, None) == "off"
        assert levels.late_scratch_below_ai(account, None) == "notify"
        assert levels.late_scratch_below_ai(account, SimpleNamespace(late_scratch_level=None)) == "notify"
        assert levels.late_scratch_below_ai(account, SimpleNamespace(late_scratch_level="act")) == "act"
        assert levels.late_scratch_below_ai(account, SimpleNamespace(late_scratch_level="off")) == "off"
