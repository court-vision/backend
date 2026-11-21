"""
Integration: one team's day on the AI desk's timeline (services/ai/timeline.py) against the real
schema. The day is ESPN's fantasy day (3 AM ET to 3 AM ET), so a check-in lands on the day it
ran; each check-in carries its calls, what Dime did about each, how a sent move went (the
ledger's row for its proposal) and the notes its own turn wrote, not a later follow-up's; the
day's check-ins still to come show only while check-ins are on; notes due are the day's live ones.
"""

import asyncio
import json
from datetime import date, datetime, time, timedelta, timezone

import pytest

from db.models.ai_conversations import AiMemory, AiProposal
from db.models.autopilot import AiCheckin, AiDecision, Autopilot, AutopilotAction
from db.models.move_outcomes import MoveOutcome
from db.models.nba.games import Game
from db.models.nba.teams import NBATeam
from db.models.teams import Team
from db.models.users import User
from services import schedule_service
from services.ai import conversations, timeline

pytestmark = [pytest.mark.integration]

DAY = date(2026, 11, 20)


def run(coro):
    return asyncio.run(coro)


def utc(d: date, hh: int, mm: int = 0) -> datetime:
    return datetime.combine(d, time(hh, mm), tzinfo=timezone.utc)


def _user(n):
    return User.create(email=f"tl{n}@courtvision.dev", clerk_user_id=f"user_tl_{n}", created_at=utc(DAY, 0))


def _team(user, name="Mine"):
    info = {"provider": "espn", "league_id": 1234, "team_name": name, "year": 2027}
    return Team.create(user_id=user.user_id, team_identifier=f"1234{name}", league_info=json.dumps(info))


def _checkin(team, user, kind, started, finished=None, nba_date=DAY, conversation=None):
    return AiCheckin.create(team_id=team.team_id, user_id=user.user_id, kind=kind, nba_date=nba_date, manual=False,
                            due_at=started, state="done", started_at=started,
                            finished_at=finished or started + timedelta(minutes=2),
                            conversation_id=conversation.id if conversation else None, cost_micros=280_000)


def _conversation(user, team, title):
    return run(conversations.create(user.user_id, team.team_id, kind=conversations.CHECKIN, title=title))


def _proposal(user, team, conv, add):
    return AiProposal.create(user_id=user.user_id, conversation_id=conv.id, team_id=team.team_id, kind="transaction",
                             day=1, scoring_period_id=33, nba_date=DAY, add_player_id=1, add_name=add,
                             drop_player_id=2, drop_name="Kris Dunn", reason="Four games", summary=f"Add {add}",
                             state="shadow", expires_at=utc(DAY, 23))


def _decision(checkin, user, team, call="hold", proposal=None, summary="Keep auto-lineup's fill"):
    return AiDecision.create(checkin_id=checkin.id, user_id=user.user_id, team_id=team.team_id, area="adds_drops",
                             automation="none", call=call, summary=summary, reason="Four games tonight",
                             proposal_id=proposal.id if proposal else None)


def _action(checkin, decision, proposal, outcome, rule=None):
    return AutopilotAction.create(team_id=checkin.team_id_id, user_id=checkin.user_id_id, checkin_id=checkin.id,
                                  decision_id=decision.id, proposal_id=proposal.id, capability="daily_actions",
                                  kind="transaction", nba_date=DAY, adds=1, outcome=outcome, rule=rule,
                                  message=f"{outcome}: Add", created_at=checkin.started_at + timedelta(minutes=1))


def _outcome(team, proposal, net):
    return MoveOutcome.create(provider="espn", provider_league_id="1234", season=2027, provider_team_id=1,
                              team_name="Mine", scoring_period_id=33, nba_date=DAY, add_player_id=proposal.id,
                              add_nba_id=10, add_name="Herb", drop_player_id=2, drop_nba_id=11, drop_name="Kris Dunn",
                              paired=True, source="dime",
                              team_id=team.team_id, proposal_id=proposal.id, format="points",
                              windows={"day": {"start": "2026-11-20", "end": "2026-11-20", "complete": True,
                                               "add": {"games": 1, "points": 30.0}, "drop": None, "net": net}},
                              net_points=net)


def _note(user, team, text, created, conversation=None, recheck_on=None, retired=False):
    return AiMemory.create(user_id=user.user_id, team_id=team.team_id if team else None, kind="player", text=text,
                           recheck_on=recheck_on, author="dime",
                           conversation_id=conversation.id if conversation else None, created_at=created,
                           retired_at=created + timedelta(minutes=5) if retired else None,
                           retired_by="dime" if retired else None, retired_reason="revised" if retired else None)


def _games():
    for t in ("DET", "BOS"):
        NBATeam.get_or_create(id=t, defaults={"name": t, "city": t, "conference": "East", "division": "Central"})
    Game.create(game_id="0022600101", game_date=DAY - timedelta(days=1), season="2026-27", home_team="DET",
                away_team="BOS", start_time_et=time(19))
    Game.create(game_id="0022600102", game_date=DAY, season="2026-27", home_team="DET", away_team="BOS",
                start_time_et=time(19, 30))


class TestTheDay:
    def test_a_day_s_check_ins_with_their_calls_moves_results_and_notes(self, integration_db, monkeypatch):
        monkeypatch.setattr(schedule_service, "get_current_matchup",
                            lambda d=None: {"start_date": DAY - timedelta(days=4)})
        _games()
        me, them = _user(1), _user(2)
        team, other = _team(me), _team(them, "Theirs")
        Autopilot.create(team_id=team.team_id, user_id=me.user_id, active_since=utc(DAY - timedelta(days=9), 12))

        # 2:30 AM ET on the 20th is still the 19th's fantasy day; 3:30 AM ET on the 21st is the 21st's
        _checkin(team, me, "event", utc(DAY, 7, 30))
        _checkin(team, me, "event", utc(DAY + timedelta(days=1), 8, 30))
        _checkin(other, them, "morning", utc(DAY, 14))

        night_conv = _conversation(me, team, "Night follow-up")
        night = _checkin(team, me, "night", utc(DAY, 11), utc(DAY, 11, 3), DAY - timedelta(days=1), night_conv)
        hold = _decision(night, me, team)
        mine = _note(me, team, "Reaves back 11-23: Barrett to IR", utc(DAY, 11, 2), night_conv)
        _note(me, team, "Written in a follow-up the user asked later", utc(DAY, 15), night_conv)

        event_conv = _conversation(me, team, "Event check-in")
        event = _checkin(team, me, "event", utc(DAY, 17, 10), utc(DAY, 17, 14), DAY, event_conv)
        sent_p, held_p = _proposal(me, team, event_conv, "Herb Jones"), _proposal(me, team, event_conv, "Ayo")
        sent = _decision(event, me, team, "change", sent_p, "Add Herb Jones for Harper")
        held = _decision(event, me, team, "change", held_p, "Add Ayo for Dunn")
        _action(event, sent, sent_p, "sent")
        _action(event, held, held_p, "blocked", "cap_day")
        _outcome(team, sent_p, 12.5)
        # The held-back chip the user sent later is in the ledger too, but it is the user's move
        _outcome(team, held_p, -3.0)

        due = _note(me, team, "Check Maxey before Saturday", utc(DAY - timedelta(days=1), 12), recheck_on=DAY)
        about_you = _note(me, None, "Fill open spots", utc(DAY - timedelta(days=2), 12), recheck_on=DAY)
        _note(me, team, "Retired", utc(DAY - timedelta(days=1), 12), recheck_on=DAY, retired=True)
        _note(me, team, "Tomorrow", utc(DAY - timedelta(days=1), 12), recheck_on=DAY + timedelta(days=1))
        _note(them, other, "Not mine", utc(DAY - timedelta(days=1), 12), recheck_on=DAY)

        out = run(timeline.day(me.user_id, team.team_id, DAY, now=utc(DAY, 12)))

        assert out["date"] == out["today"] == DAY
        assert (out["starts_at"], out["ends_at"]) == (utc(DAY, 8), utc(DAY + timedelta(days=1), 8))
        assert out["checkins_on"] is True
        assert [c["checkin_id"] for c in out["checkins"]] == [night.id, event.id]

        n = out["checkins"][0]
        assert n["kind"] == "night" and n["date"] == DAY - timedelta(days=1)
        assert [c["decision_id"] for c in n["calls"]] == [hold.id]
        assert n["calls"][0]["action"] is None and n["calls"][0]["result"] is None
        assert [x["note_id"] for x in n["notes"]] == [mine.id]

        e = out["checkins"][1]
        assert e["decisions"] == 2 and e["changes"] == 2 and e["sent"] == 1
        by_id = {c["decision_id"]: c for c in e["calls"]}
        assert by_id[sent.id]["action"]["outcome"] == "sent"
        assert by_id[sent.id]["result"]["windows"]["day"]["net"] == 12.5
        assert by_id[sent.id]["result"]["source"] == "dime" and by_id[sent.id]["result"]["mine"] is True
        assert by_id[held.id]["action"]["rule"] == "cap_day" and by_id[held.id]["result"] is None

        assert {x["note_id"] for x in out["notes_due"]} == {due.id, about_you.id}
        # 7 AM ET: the night follow-up ran, the morning (9 AM ET) and pre-lock (45 min before a 7:30 PM tip) are to come
        assert [(s["kind"], s["date"]) for s in out["upcoming"]] == [("morning", DAY), ("pre_lock", DAY)]
        assert out["upcoming"][1]["due_at"] == utc(DAY + timedelta(days=1), 0) - timedelta(minutes=15)
        assert out["first_tip"] == utc(DAY + timedelta(days=1), 0, 30)

    def test_nothing_is_to_come_on_a_past_day_or_while_check_ins_are_off(self, integration_db, monkeypatch):
        monkeypatch.setattr(schedule_service, "get_current_matchup",
                            lambda d=None: {"start_date": DAY - timedelta(days=4)})
        _games()
        me = _user(1)
        team = _team(me)
        # Never switched on: no row, so off
        off = run(timeline.day(me.user_id, team.team_id, DAY, now=utc(DAY, 12)))
        assert off["checkins_on"] is False and off["upcoming"] == [] and off["checkins"] == []
        assert off["first_tip"] == utc(DAY + timedelta(days=1), 0, 30)

        Autopilot.create(team_id=team.team_id, user_id=me.user_id, active_since=utc(DAY - timedelta(days=9), 12),
                         paused_at=utc(DAY, 9))
        paused = run(timeline.day(me.user_id, team.team_id, DAY, now=utc(DAY, 12)))
        assert paused["checkins_on"] is False and paused["upcoming"] == []

        Autopilot.update(paused_at=None).where(Autopilot.team_id == team.team_id).execute()
        past = run(timeline.day(me.user_id, team.team_id, DAY, now=utc(DAY + timedelta(days=1), 12)))
        assert past["today"] == DAY + timedelta(days=1) and past["checkins_on"] is True and past["upcoming"] == []
        # Without a date it is today's fantasy day: before 3 AM ET, still yesterday's
        early = run(timeline.day(me.user_id, team.team_id, now=utc(DAY + timedelta(days=1), 7)))
        assert early["date"] == DAY
