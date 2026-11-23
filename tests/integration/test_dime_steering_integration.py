"""
Integration: Dime's steering against the real schema (migration 0043).

The holds table's own checks (a kind, a share only an override carries, a run of
days that ends within four weeks, a reason), the live read (cleared ones and those
past their last day left out, so a hold never outlives its date), applying a steer
chip (its holds made and its clears done in one transaction, the chip marked sent,
a second apply making nothing), clearing one, the pins a sent lineup chip leaves
for the players it benched, and the steering kinds' rows on usr.ai_proposals
(an `action` each, the old kinds unchanged).
"""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone

import pytest
from peewee import IntegrityError

from core.errors import ConflictError
from db.base import db
from db.models.ai_conversations import AiProposal
from db.models.team_holds import TeamHold, live_holds
from db.models.teams import Team
from db.models.users import User
from services import steering
from services.ai import conversations
from services.ai import steering as ai_steering

pytestmark = [pytest.mark.integration]

DAY = date(2026, 11, 10)


def run(coro):
    return asyncio.run(coro)


def _now():
    return datetime.now(timezone.utc)


@pytest.fixture
def team():
    user = User.create(email="steer@courtvision.dev", clerk_user_id="user_steer", created_at=_now())
    info = {"provider": "espn", "league_id": 1234, "team_name": "Mine", "year": 2027}
    return Team.create(user_id=user.user_id, team_identifier="1234Mine", league_info=json.dumps(info))


def _hold(team, **kw):
    fields = dict(team_id=team.team_id, user_id=team.user_id_id, kind="keep", player_id=4594268,
                  player_name="Anthony Edwards", starts_on=DAY, ends_on=DAY + timedelta(days=2), reason="Top-20")
    fields.update(kw)
    return TeamHold.create(**fields)


def _chip(team, kind="steer", action=None, **kw):
    conv = run(conversations.create(team.user_id_id, team.team_id, title="Steer"))
    fields = dict(user_id=team.user_id_id, conversation_id=conv.id, team_id=team.team_id, kind=kind, day=1,
                  scoring_period_id=20, nba_date=DAY, reason="Edwards is the team", summary="Keep Anthony Edwards",
                  state="proposed", expires_at=_now() + timedelta(hours=8), action=action)
    fields.update(kw)
    return AiProposal.create(**fields)


class TestTheTable:
    def test_each_kind_carries_what_it_needs(self, integration_db, team):
        _hold(team)
        _hold(team, kind="override", share=0.5)
        _hold(team, kind="override", share=0)
        for bad in (dict(kind="trade"), dict(kind="override"), dict(kind="keep", share=0.5),
                    dict(kind="override", share=2.5), dict(reason=""),
                    dict(ends_on=DAY - timedelta(days=1)), dict(ends_on=DAY + timedelta(days=29)),
                    dict(applied_by="robot")):
            with pytest.raises(IntegrityError), db.atomic():
                _hold(team, **bad)
        with pytest.raises(IntegrityError), db.atomic():
            _hold(team, cleared_at=_now())                          # cleared says by whom
        _hold(team, ends_on=DAY + timedelta(days=28))

    def test_a_hold_is_read_through_its_last_day_and_never_after_nor_once_cleared(self, integration_db, team):
        a = _hold(team, starts_on=DAY + timedelta(days=1))
        b = _hold(team, kind="block", player_id=6450, player_name="Kawhi Leonard", ends_on=DAY)
        _hold(team, kind="pin", ends_on=DAY - timedelta(days=1), starts_on=DAY - timedelta(days=1))
        _hold(team, kind="block", player_id=1, player_name="Gone", cleared_at=_now(), cleared_by="user")
        assert [h.id for h in live_holds(team.team_id, DAY)] == [b.id, a.id]          # by start day
        assert [h.id for h in live_holds(team.team_id, DAY + timedelta(days=1))] == [a.id]
        assert live_holds(team.team_id, DAY + timedelta(days=3)) == []

    def test_a_team_s_holds_go_with_it(self, integration_db, team):
        _hold(team)
        Team.delete().where(Team.team_id == team.team_id).execute()
        assert TeamHold.select().count() == 0


class TestTheChips:
    def test_the_steering_kinds_carry_an_action_and_the_old_ones_their_players(self, integration_db, team):
        _chip(team, "steer", {"holds": [], "clear": []})
        _chip(team, "lane", {"op": "stop", "lane_id": 3})
        _chip(team, "cancel_pickup", {"pickup_id": 9})
        with pytest.raises(IntegrityError), db.atomic():
            _chip(team, "lane", None)
        with pytest.raises(IntegrityError), db.atomic():
            _chip(team, "trade", {"x": 1})
        _chip(team, "transaction", None, add_player_id=6450, add_name="Kawhi Leonard")

    def test_applying_a_steer_chip_makes_its_holds_and_clears_once(self, integration_db, team, monkeypatch):
        monkeypatch.setattr(ai_steering.schedule_service, "get_nba_today", lambda: DAY)
        old = _hold(team, kind="block", player_id=6450, player_name="Kawhi Leonard")
        chip = _chip(team, action={
            "holds": [
                {"kind": "override", "player": {"espn_id": 4594268, "nba_player_id": 1630162, "name": "Anthony Edwards",
                                                "team": "MIN"}, "share": 0.0, "starts_on": (DAY - timedelta(days=1)).isoformat(),
                 "ends_on": (DAY + timedelta(days=1)).isoformat(), "effect": "Count him out"},
                {"kind": "keep", "player": {"espn_id": 1, "nba_player_id": None, "name": "Past", "team": None},
                 "share": None, "starts_on": (DAY - timedelta(days=2)).isoformat(),
                 "ends_on": (DAY - timedelta(days=1)).isoformat(), "effect": "Keep"},
            ],
            "clear": [{"hold_id": old.id, "kind": "block"}]})
        owned = type("T", (), {"team_id": team.team_id, "user_id": team.user_id_id})()
        assert run(ai_steering.apply_holds(owned, chip.id)) == 1                 # the past one is left out
        (made,) = live_holds(team.team_id, DAY)
        assert (made.kind, float(made.share), made.starts_on, made.reason, made.proposal_id_id, made.applied_by) == \
            ("override", 0.0, DAY, "Edwards is the team", chip.id, "user")
        cleared = TeamHold.get_by_id(old.id)
        assert cleared.cleared_at is not None and cleared.cleared_by == "user"
        assert AiProposal.get_by_id(chip.id).state == "sent"
        with pytest.raises(ConflictError):
            run(ai_steering.apply_holds(owned, chip.id))
        assert TeamHold.select().count() == 2
        # The planners now read it
        got = run(steering.holds_for(team.team_id, DAY))
        assert [(h.kind, h.share, h.player_id) for h in got] == [("override", 0.0, 4594268)]

    def test_a_shadow_or_expired_chip_is_never_applied(self, integration_db, team):
        owned = type("T", (), {"team_id": team.team_id, "user_id": team.user_id_id})()
        for chip in (_chip(team, action={"holds": [], "clear": []}, state="shadow"),
                     _chip(team, action={"holds": [], "clear": []}, expires_at=_now() - timedelta(minutes=1))):
            with pytest.raises(ConflictError):
                run(ai_steering.apply_holds(owned, chip.id))
        assert TeamHold.select().count() == 0

    def test_a_hold_is_cleared_once_and_only_by_its_team(self, integration_db, team):
        h = _hold(team)
        assert run(steering.clear(team.team_id + 1, h.id)) is None
        first = run(steering.clear(team.team_id, h.id, by="user"))
        assert first.cleared_by == "user"
        again = run(steering.clear(team.team_id, h.id, by="dime"))
        assert again.cleared_by == "user" and again.cleared_at == first.cleared_at

    def test_a_sent_lineup_chip_pins_the_players_it_benched_for_its_day(self, integration_db, team):
        chip = _chip(team, "lineup", None, reason="Rest night for Edwards", moves=[
            {"espn_id": 4594268, "nba_player_id": 1630162, "name": "Anthony Edwards", "team": "MIN", "role": "bench",
             "from_slot_id": 0, "from_slot": "PG", "to_slot_id": 12, "to_slot": "BE"},
            {"espn_id": 6450, "name": "Kawhi Leonard", "role": "start", "from_slot_id": 12, "from_slot": "BE",
             "to_slot_id": 0, "to_slot": "PG"}])
        run(steering.pin_benched(chip.id))
        (pin,) = live_holds(team.team_id, DAY)
        assert (pin.kind, pin.player_id, pin.starts_on, pin.ends_on, pin.reason, pin.proposal_id_id) == \
            ("pin", 4594268, DAY, DAY, "Rest night for Edwards", chip.id)
