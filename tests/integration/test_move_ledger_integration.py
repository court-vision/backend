"""
The move ledger against real SQL: lineup snapshots, Court Vision's writes and box
scores in, usr.move_outcomes (migration 0041) out, read back the way the AI desk
and Dime read it.
"""

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from freezegun import freeze_time

from db.models.ai_conversations import AiConversation, AiProposal
from db.models.lineup_snapshots import LineupSnapshot, LineupSnapshotPlayer
from db.models.move_outcomes import MoveOutcome
from db.models.nba.player_game_stats import PlayerGameStats
from db.models.nba.players import Player
from db.models.roster_moves import RosterMove
from db.models.scheduled_pickups import ScheduledPickup
from db.models.teams import Team
from db.models.users import User
from schemas.move_outcomes import LedgerRunReq
from services import move_ledger as ml
from services.move_ledger_service import MoveLedgerService

pytestmark = pytest.mark.integration

OPENING = date(2026, 10, 20)
KEY = ml.LeagueKey("espn", "1001", 2027)


def run(coro):
    return asyncio.run(coro)


def _day(period: int) -> date:
    return OPENING + timedelta(days=period - 1)


def _stored(team: int, name: str, period: int, players: dict[int, int]) -> None:
    header = LineupSnapshot.create(
        provider="espn", provider_league_id=KEY.provider_league_id, season=KEY.season, provider_team_id=team,
        team_name=name, scoring_period_id=period, nba_date=_day(period), matchup_period_id=1,
        player_count=len(players),
    )
    for pid, slot in players.items():
        LineupSnapshotPlayer.create(snapshot=header, player_id=pid, player_name=f"P{pid}", lineup_slot_id=slot)


def _write(user, team, period, moves, source="manual") -> RosterMove:
    return RosterMove.create(user=user.user_id, team=team.team_id, nba_date=_day(period), scoring_period_id=period,
                             source=source, kind="transaction", status="applied", moves=moves)


@pytest.fixture
def league(integration_db):
    user = User.create(email="atlas@courtvision.dev", clerk_user_id="user_atlas",
                       created_at=datetime.now(timezone.utc))
    info = {"provider": "espn", "league_id": 1001, "team_name": "Atlas", "year": 2027, "espn_team_id": 1}
    team = Team.create(user_id=user.user_id, team_identifier="1001Atlas", league_info=json.dumps(info))
    for espn_id in (101, 102, 103, 104, 105, 106, 201, 202, 203):
        Player.create(id=9000 + espn_id, espn_id=espn_id, name=f"P{espn_id}", name_normalized=f"p{espn_id}")

    # Atlas: day 2 the desk swaps 103 for 104; day 3 a scheduled pickup swaps 102 for 105
    # (sent on day 2, the day before); day 4 Dime's proposal, sent, swaps 104 for 106.
    _stored(1, "Atlas", 1, {101: 0, 102: 1, 103: 12})
    _stored(1, "Atlas", 2, {101: 0, 102: 1, 104: 12})
    _stored(1, "Atlas", 3, {101: 0, 105: 1, 104: 2})
    _stored(1, "Atlas", 4, {101: 0, 105: 1, 106: 2})
    # The rival swaps 202 for 203 in ESPN on day 2.
    _stored(2, "Rival", 1, {201: 0, 202: 0})
    _stored(2, "Rival", 2, {201: 0, 203: 0})
    _stored(2, "Rival", 3, {201: 0, 203: 0})

    _write(user, team, 2, [{"player_id": 104, "action": "add"}, {"player_id": 103, "action": "drop"}])
    pickup_write = _write(user, team, 2, [{"player_id": 105, "action": "add"}, {"player_id": 102, "action": "drop"}],
                          source="scheduled")
    ScheduledPickup.create(user=user.user_id, team=team.team_id, add_player_id=105, drop_player_id=102,
                           add_name="P105", drop_name="P102", scoring_period_id=3, nba_date=_day(3),
                           not_before_at=datetime(2026, 10, 21, tzinfo=timezone.utc), status="executed",
                           audit_id=pickup_write.id)
    dime_write = _write(user, team, 4, [{"player_id": 106, "action": "add"}, {"player_id": 104, "action": "drop"}])
    conversation = AiConversation.create(user_id=user.user_id, team_id=team.team_id)
    AiProposal.create(user_id=user.user_id, conversation_id=conversation.id, team_id=team.team_id,
                      kind="transaction", day=3, scoring_period_id=4, nba_date=_day(4),
                      add_player_id=106, add_nba_id=9106, add_name="P106",
                      drop_player_id=104, drop_nba_id=9104, drop_name="P104",
                      reason="more games this week", summary="Add P106, drop P104",
                      projection={"scoring": "points", "change": 7.5}, state="sent",
                      expires_at=datetime(2026, 10, 24, tzinfo=timezone.utc), audit_id=dime_write.id)

    # Box scores: 104 plays days 2 and 3 (started on day 3 only), 103 plays day 2.
    PlayerGameStats.upsert_game_stats(9104, _day(2), {"min": 30, "pts": 20, "reb": 5, "fpts": 25})
    PlayerGameStats.upsert_game_stats(9104, _day(3), {"min": 30, "pts": 10, "reb": 0, "fpts": 10})
    PlayerGameStats.upsert_game_stats(9103, _day(2), {"min": 30, "pts": 6, "reb": 0, "fpts": 6})
    return SimpleNamespace(user=user, team=team)


def _owned(team) -> SimpleNamespace:
    return SimpleNamespace(team_id=team.team_id, user_id=team.user_id_id, league_id=None, league=None,
                           league_info_json=team.league_info)


def test_the_rebuild_finds_every_move_and_who_made_it(league):
    done = ml.rebuild_league(KEY)
    assert (done.days, done.moves) == (7, 4)
    assert done.by_source == {"you": 1, "scheduled": 1, "dime": 1, "outside": 1}

    rows = {(r.provider_team_id, r.scoring_period_id): r for r in MoveOutcome.select()}
    desk = rows[(1, 2)]
    assert (desk.source, desk.add_nba_id, desk.drop_nba_id, desk.paired, desk.team_id_id) == (
        "you", 9104, 9103, True, league.team.team_id)
    assert desk.windows["day"]["add"]["games"] == 1 and desk.windows["day"]["add"]["counted_games"] == 0   # benched
    assert desk.windows["two_weeks"]["add"]["counted_games"] == 1                                            # day 3
    assert desk.windows["two_weeks"]["complete"] is False
    assert desk.windows["held_through"] == _day(3).isoformat()
    assert float(desk.net_points) == desk.windows["two_weeks"]["net"] > 0

    assert rows[(1, 3)].source == "scheduled"
    dime = rows[(1, 4)]
    assert (dime.source, dime.forecast, dime.reason) == ("dime", {"scoring": "points", "change": 7.5},
                                                         "more games this week")
    rival = rows[(2, 2)]
    assert (rival.source, rival.team_id_id, rival.add_nba_id, rival.paired) == ("outside", None, 9203, True)


def test_a_rerun_replaces_the_league_s_rows(league):
    ml.rebuild_league(KEY)
    first = sorted((r.provider_team_id, r.scoring_period_id) for r in MoveOutcome.select())
    ml.rebuild_league(KEY)
    assert sorted((r.provider_team_id, r.scoring_period_id) for r in MoveOutcome.select()) == first


@freeze_time("2026-10-25T17:00:00Z")
def test_the_job_rebuilds_the_active_leagues_and_the_desk_reads_them(league):
    data = run(MoveLedgerService.run(LedgerRunReq())).data
    assert [(r.provider_league_id, r.season, r.ok, r.moves) for r in data.leagues] == [("1001", 2027, True, 4)]

    mine = run(MoveLedgerService.for_team(_owned(league.team))).data
    assert (mine.scope, mine.format, mine.total) == ("team", "points", 3)
    assert [m.date for m in mine.moves] == [_day(4).isoformat(), _day(3).isoformat(), _day(2).isoformat()]
    assert {s.source: s.moves for s in mine.summary} == {"dime": 1, "scheduled": 1, "you": 1}

    everyone = run(MoveLedgerService.for_team(_owned(league.team), scope="league")).data
    assert everyone.total == 4
    assert [m.mine for m in everyone.moves if m.team_name == "Rival"] == [False]

    by_player = run(MoveLedgerService.for_team(_owned(league.team), nba_player_id=9104)).data
    assert sorted(m.date for m in by_player.moves) == [_day(2).isoformat(), _day(4).isoformat()]
    only_dime = run(MoveLedgerService.for_team(_owned(league.team), sources=("dime",))).data
    assert [m.source for m in only_dime.moves] == ["dime"]


def test_a_team_s_scoring_preview_does_not_score_the_league(league):
    info = json.loads(league.team.league_info)
    info["scoring_preview"] = "categories"
    Team.update(league_info=json.dumps(info)).where(Team.team_id == league.team.team_id).execute()

    ml.rebuild_league(KEY)
    assert {r.format for r in MoveOutcome.select()} == {"points"}


def test_a_past_season_s_write_does_not_explain_this_season_s_move(league):
    # The desk's day-2 write is gone; last season's write on the same team, period and
    # players must not stand in for it (scoring periods restart each season).
    RosterMove.delete().where((RosterMove.scoring_period_id == 2) & (RosterMove.source == "manual")).execute()
    RosterMove.create(user=league.user.user_id, team=league.team.team_id, nba_date=_day(2) - timedelta(days=365),
                      scoring_period_id=2, source="manual", kind="transaction", status="applied",
                      moves=[{"player_id": 104, "action": "add"}, {"player_id": 103, "action": "drop"}])

    ml.rebuild_league(KEY)
    row = MoveOutcome.get((MoveOutcome.provider_team_id == 1) & (MoveOutcome.scoring_period_id == 2))
    assert row.source == "outside"


@freeze_time("2026-10-25T17:00:00Z")
def test_the_job_rebuilds_every_active_league(league):
    other = ml.LeagueKey("espn", "2002", 2027)
    LineupSnapshot.create(provider="espn", provider_league_id=other.provider_league_id, season=other.season,
                          provider_team_id=1, team_name="Elsewhere", scoring_period_id=1, nba_date=_day(1),
                          matchup_period_id=1, player_count=0)

    data = run(MoveLedgerService.run(LedgerRunReq())).data
    assert [(r.provider_league_id, r.ok) for r in data.leagues] == [("1001", True), ("2002", True)]
    assert (data.rebuilt, data.failed) == (2, 0)


def _rival_on_court_vision(league) -> None:
    """The rival's day-2 swap was a stream lane's pickup on another Court Vision user's team."""
    other = User.create(email="rival@example.com", clerk_user_id="user_rival", created_at=datetime.now(timezone.utc))
    info = {"provider": "espn", "league_id": 1001, "team_name": "Rival", "year": 2027, "espn_team_id": 2}
    team = Team.create(user_id=other.user_id, team_identifier="1001Rival", league_info=json.dumps(info))
    _write(other, team, 2, [{"player_id": 203, "action": "add"}, {"player_id": 202, "action": "drop"}])


def test_a_rival_s_court_vision_source_stays_theirs(league):
    _rival_on_court_vision(league)
    ml.rebuild_league(KEY)
    assert MoveOutcome.get(MoveOutcome.provider_team_id == 2).source == "you"      # stored as theirs

    everyone = run(MoveLedgerService.for_team(_owned(league.team), scope="league")).data
    rival = next(m for m in everyone.moves if m.team_name == "Rival")
    assert rival.mine is False and rival.source == "outside" and rival.reason is None
    assert {s.source: s.moves for s in everyone.summary} == {"dime": 1, "scheduled": 1, "you": 1, "outside": 1}

    # Filtering by source goes by what the asking team sees, too
    outside = run(MoveLedgerService.for_team(_owned(league.team), scope="league", sources=("outside",))).data
    assert [m.team_name for m in outside.moves] == ["Rival"]
    yours = run(MoveLedgerService.for_team(_owned(league.team), scope="league", sources=("you",))).data
    assert [(m.team_name, m.source) for m in yours.moves] == [("Atlas", "you")]


def test_league_activity_regroups_the_ledger_by_team(league):
    from services.ai import league as ai_league

    _rival_on_court_vision(league)
    Player.update(position="G").where(Player.id.in_([9104, 9105])).execute()
    Player.update(position="C").where(Player.id == 9203).execute()
    ml.rebuild_league(KEY)

    args = ai_league.LeagueActivityInput(team_id=league.team.team_id, days=7)
    out = run(ai_league.get_league_activity(_owned(league.team), args, today=_day(5)))
    assert out["through"] == _day(4).isoformat() and out["total"] == 4
    assert [(m["date"], m["team_name"], m["mine"]) for m in out["moves"]] == [
        (_day(4).isoformat(), "Atlas", True), (_day(3).isoformat(), "Atlas", True),
        (_day(2).isoformat(), "Rival", False), (_day(2).isoformat(), "Atlas", True),
    ]
    desk = out["moves"][3]
    assert desk["source"] == "you" and desk["add"] == {"player_id": 9104, "name": "P104", "position": "G"}
    assert desk["still_held"] is False and desk["held_through"] == _day(3).isoformat()
    rival = out["moves"][2]
    # The rival's last stored day is day 3, and 203 is still there on it
    assert "source" not in rival and rival["still_held"] is True
    assert {t["team_name"]: (t["adds"], t["drops"], t["positions_added"]) for t in out["by_team"]} == {
        "Atlas": (3, 3, "G 2"), "Rival": (1, 1, "C 1"),
    }

    rival_only = run(ai_league.get_league_activity(
        _owned(league.team), ai_league.LeagueActivityInput(team_id=league.team.team_id, league_team_id=2),
        today=_day(5)))
    assert [m["team_name"] for m in rival_only["moves"]] == ["Rival"]
    recent = run(ai_league.get_league_activity(
        _owned(league.team), ai_league.LeagueActivityInput(team_id=league.team.team_id, days=2), today=_day(5)))
    assert [m["date"] for m in recent["moves"]] == [_day(4).isoformat(), _day(3).isoformat()]
