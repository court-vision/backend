"""
Integration: the late-scratch swap's reads and writes against the real schema
(services/late_scratch_service.py). Which move answers a verdict is tested in
tests/unit/test_late_scratch_service.py; here are the teams it asks, the verdicts
it reads (and the Late scratches level that picks them), the swaps it counts, and the audit row the write chain seeds with the
player whose verdict it answers.
"""

import json
from datetime import date, datetime, timezone

import pytest
from peewee import IntegrityError

from db.models.autopilot import Autopilot
from db.models.nba import Player, PlayerAvailability
from db.models.notifications import NotificationPreference, NotificationTeamPreference
from db.models.roster_moves import RosterMove
from db.models.teams import Team
from db.models.users import User
from services import late_scratch_service as late
from services import lineup_editor_service as svc

pytestmark = [pytest.mark.integration]

DAY = date(2026, 11, 10)
BRUNSON, HART = 1628973, 1628404


@pytest.fixture
def team(integration_db):
    for pid, name in ((BRUNSON, "Jalen Brunson"), (HART, "Josh Hart")):
        Player.create(id=pid, name=name, name_normalized=name.lower(),
                      created_at=datetime.utcnow(), updated_at=datetime.utcnow())
    user = User.create(email="scratch@courtvision.dev", clerk_user_id="user_scratch", created_at=datetime.utcnow())
    info = {"provider": "espn", "league_id": 552315826, "team_name": "Test", "year": 2027}
    return Team.create(user_id=user.user_id, team_identifier="552315826Test", league_info=json.dumps(info))


def _swap(team, trigger, status="applied", day=DAY):
    audit_id = svc._audit_insert(team.user_id_id, team.team_id, day, 24, "availability",
                                 [{"player_id": 3, "from_slot_id": 12, "to_slot_id": 11}], "k",
                                 trigger_player_id=trigger)
    svc._audit_update(audit_id, status)
    return audit_id


def _second_team(user_id, name="Second"):
    info = {"provider": "espn", "league_id": 1, "team_name": name, "year": 2027}
    return Team.create(user_id=user_id, team_identifier=f"1{name}", league_info=json.dumps(info))


def test_teams_at_notify_or_act_are_asked_by_override_else_account(team):
    other = _second_team(team.user_id_id)
    third = _second_team(team.user_id_id, "Third")
    assert late._teams() == []                      # every level defaults to off

    NotificationPreference.create(user=team.user_id_id, email="scratch@courtvision.dev", late_scratch_level="notify")
    NotificationTeamPreference.create(user=team.user_id_id, team_id=team.team_id, late_scratch_level="act")
    NotificationTeamPreference.create(user=team.user_id_id, team_id=third.team_id, late_scratch_level="off")
    assert late._teams() == [(team.team_id, team.user_id_id, "act"), (other.team_id, team.user_id_id, "notify")]


def test_a_team_override_counts_with_the_account_off(team):
    NotificationTeamPreference.create(user=team.user_id_id, team_id=team.team_id, late_scratch_level="notify")
    assert late._teams() == [(team.team_id, team.user_id_id, "notify")]


def test_a_team_at_ai_is_left_to_dime(team):
    NotificationTeamPreference.create(user=team.user_id_id, team_id=team.team_id, late_scratch_level="act")
    now = datetime.now(timezone.utc)
    Autopilot.create(team_id=team.team_id, user_id=team.user_id_id, ai_rows=["late_scratch"], created_at=now,
                     updated_at=now, active_since=now)
    assert late._teams() == []

    Autopilot.update(ai_rows=["lineup"]).where(Autopilot.team_id == team.team_id).execute()
    assert late._teams() == [(team.team_id, team.user_id_id, "act")]
    Autopilot.update(autopilot=True).where(Autopilot.team_id == team.team_id).execute()
    assert late._teams() == []


def test_the_level_is_written_and_read_through_the_settings_table(team):
    from services.ai import autonomy, levels
    now = datetime.now(timezone.utc)
    NotificationPreference.create(user=team.user_id_id, email="scratch@courtvision.dev", lineup_alerts_enabled=True)
    levels.apply.__wrapped__(team.team_id, team.user_id_id, levels.Change(levels={"late_scratch": "act"}), now)
    prefs, overrides = levels.prefs_of.__wrapped__(team.user_id_id, [team.team_id])
    assert overrides[team.team_id].late_scratch_level == "act"
    assert levels.late_scratch_below_ai(prefs, overrides[team.team_id]) == "act"
    assert late._teams() == [(team.team_id, team.user_id_id, "act")]

    # At AI nothing is written to the override, so coming back down finds what the user had
    row = levels.apply.__wrapped__(team.team_id, team.user_id_id, levels.Change(levels={"late_scratch": "ai"}), now)
    assert autonomy.at_ai(row, autonomy.LATE_SCRATCH)
    assert NotificationTeamPreference.get(NotificationTeamPreference.team_id == team.team_id).late_scratch_level == "act"
    assert late._teams() == []


def test_the_days_verdicts_with_the_one_before(team):
    PlayerAvailability.create(player=BRUNSON, game_date=DAY, verdict="out", previous_verdict="questionable",
                              decided_by="espn_injuries")
    PlayerAvailability.create(player=HART, game_date=date(2026, 11, 9), verdict="out", decided_by="espn_injuries")

    assert late._verdicts([BRUNSON, HART], DAY) == {BRUNSON: ("out", "questionable")}
    assert late._verdicts([], DAY) == {}


def test_the_write_chain_seeds_the_trigger_and_swaps_are_counted_per_day(team):
    audit_id = _swap(team, BRUNSON)
    assert RosterMove.get_by_id(audit_id).trigger_player_id == BRUNSON
    _swap(team, HART, status="rejected")              # a refused write is not a swap
    _swap(team, HART, day=date(2026, 11, 9))         # yesterday's is not today's

    assert late._swaps_today(team.team_id, DAY) == (1, {BRUNSON})


def test_a_second_counted_swap_for_the_same_player_is_refused(team):
    _swap(team, BRUNSON)
    with pytest.raises(IntegrityError):
        _swap(team, BRUNSON)


def test_the_matchups_verdicts_by_player_then_day(team):
    from services import availability_service
    PlayerAvailability.create(player=HART, game_date=DAY, verdict="doubtful", decided_by="nba_report",
                              reason="Right Hamstring; Strain")
    PlayerAvailability.create(player=BRUNSON, game_date=date(2026, 11, 12), verdict="available",
                              decided_by="espn_injuries")
    PlayerAvailability.create(player=BRUNSON, game_date=DAY, verdict="out", decided_by="espn_injuries",
                              previous_verdict="questionable", reason="Left Ankle; Sprain")
    PlayerAvailability.create(player=BRUNSON, game_date=date(2026, 11, 20), verdict="out", decided_by="espn_injuries")

    found = availability_service.rows([HART, BRUNSON, 99999], DAY, date(2026, 11, 16))
    assert [(v.nba_player_id, v.game_date, v.verdict) for v in found] == [
        (HART, "2026-11-10", "doubtful"), (BRUNSON, "2026-11-10", "out"), (BRUNSON, "2026-11-12", "available")]
    assert found[1].changed_at.tzinfo is not None and found[1].reason == "Left Ankle; Sprain"
    assert availability_service.planner_verdicts(found, DAY) == {HART: ("doubtful", None),
                                                                 BRUNSON: ("out", "questionable")}
    assert availability_service.rows([], DAY, DAY) == []
