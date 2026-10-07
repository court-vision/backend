"""
`services.lineup_snapshot_service`: rows and ESPN history in the snapshot
shape, which team a caller may read, and when the live fallback is used.
`run_db` runs its function inline; the table and ESPN are faked.
"""

import asyncio
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from freezegun import freeze_time

from core.errors import BadRequestError, NotFoundError
from services import lineup_snapshot_service as svc
from services import matchup_days

TODAY = date(2026, 11, 10)
YESTERDAY = date(2026, 11, 9)
PERIOD = 21                      # 2026-11-09 is ESPN day 21 of 2026-27
OWNER = SimpleNamespace(
    team_id=7, user_id=42,
    league_info_json='{"provider": "espn", "league_id": 552315826, "team_name": "GloatingSoap369", "year": 2027, "espn_team_id": 1}',
    league_id=None, league=None,
)
OWNER_NO_ID = SimpleNamespace(
    team_id=7, user_id=42,
    league_info_json='{"provider": "espn", "league_id": 552315826, "team_name": "GloatingSoap369", "year": 2027}',
    league_id=None, league=None,
)
YAHOO = SimpleNamespace(team_id=8, user_id=42, league_info_json='{"provider": "yahoo", "league_id": 1, "team_name": "Y", "year": 2027, "yahoo_team_key": "466.l.1.t.1"}', league_id=None, league=None)


def header(team_id=1, name="GloatingSoap369", nba_date=YESTERDAY, period=PERIOD):
    return SimpleNamespace(
        id=99, provider="espn", provider_league_id="552315826", season=2027, provider_team_id=team_id,
        team_name=name, scoring_period_id=period, nba_date=nba_date, matchup_period_id=4,
        opponent_provider_team_id=5, applied_stat_total=145.0, player_count=2,
        captured_at=datetime(2026, 11, 10, 8, 5, tzinfo=timezone.utc), source="pipeline",
    )


def row(pid, slot, name, pos=1, team="DET", applied=None, eligible=(0, 5, 11, 12, 13)):
    return SimpleNamespace(snapshot_id=99, player_id=pid, player_name=name, pro_team=team, default_position_id=pos,
                           lineup_slot_id=slot, eligible_slot_ids=list(eligible), injured=False,
                           injury_status="ACTIVE", applied_total=applied)


def espn_payload(latest=PERIOD + 1, teams=((1, "GloatingSoap369"), (5, "Rival"))):
    return {
        "scoringPeriodId": PERIOD,
        "status": {"latestScoringPeriod": latest, "finalScoringPeriod": 167},
        "settings": {"rosterSettings": {"lineupSlotCounts": {"0": 1, "11": 1, "12": 1}}},
        "teams": [{
            "id": tid, "name": name,
            "roster": {"entries": [{
                "playerId": 100 + tid, "lineupSlotId": 0,
                "playerPoolEntry": {"id": 100 + tid, "lineupLocked": True,
                                    "player": {"id": 100 + tid, "fullName": f"Starter {tid}", "proTeamId": 7,
                                               "eligibleSlots": [0, 11, 12], "injured": False,
                                               "injuryStatus": "ACTIVE", "defaultPositionId": 1}},
            }, {
                "playerId": 200 + tid, "lineupSlotId": 12,
                "playerPoolEntry": {"id": 200 + tid, "lineupLocked": False,
                                    "player": {"id": 200 + tid, "fullName": f"Bench {tid}", "proTeamId": 7,
                                               "eligibleSlots": [4, 11, 12], "injured": True,
                                               "injuryStatus": "OUT", "defaultPositionId": 5}},
            }]},
        } for tid, name in teams],
    }


@pytest.fixture
def world(monkeypatch):
    """A fake table, a fake ESPN, inline run_db, and a fixed calendar."""
    w = SimpleNamespace(rows={}, espn_calls=[], payload=espn_payload(), persisted=[], hydrated=0)

    async def direct_run_db(name, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def load(ref, team_ids, dates):
        return {k: v for k, v in w.rows.items() if k[0] in team_ids and k[1] in dates}

    def by_name(ref, dates, team_name):
        newest_first = sorted(w.rows.items(), key=lambda kv: kv[0][1], reverse=True)
        return next((tid for (tid, d), snap in newest_first if d in dates and snap.team_name == team_name), None)

    async def fetch(league_info, views, *, expect_key="teams", scoring_period_id=None):
        w.espn_calls.append((tuple(views), scoring_period_id))
        return w.payload

    async def hydrate(team):
        w.hydrated += 1
        return svc.public_league_info(team)

    monkeypatch.setattr(svc, "run_db", direct_run_db)
    monkeypatch.setattr(svc, "_load_snapshots", load)
    monkeypatch.setattr(svc, "_team_id_by_name", by_name)
    monkeypatch.setattr(svc, "_persist_espn_team_id", lambda team_id, espn_team_id: w.persisted.append((team_id, espn_team_id)) or True)
    monkeypatch.setattr(svc, "nba_ids_by_espn_id", lambda ids: {i: 9000 + i for i in ids})
    monkeypatch.setattr(svc.EspnService, "fetch_league", staticmethod(fetch))
    monkeypatch.setattr(svc, "load_owned_league_info", hydrate)
    monkeypatch.setattr(svc, "fantasy_today", lambda: TODAY)
    monkeypatch.setattr(svc.schedule_service, "season_day", lambda d=None, season=None: (d - date(2026, 10, 20)).days + 1)
    monkeypatch.setattr(svc.schedule_service, "get_current_matchup",
                        lambda d=None: {"matchup_number": 4, "start_date": date(2026, 11, 9), "end_date": date(2026, 11, 15)})
    return w


def stored(w, team_id=1, name="GloatingSoap369", nba_date=YESTERDAY):
    snap = svc.snapshot_from_rows(
        header(team_id, name, nba_date),
        [row(2, 12, "Benchwarmer", applied=None), row(1, 0, "Cade Cunningham", applied=48.0), row(3, 13, "Hurt Guy", pos=5)],
        {1: 4432166},
    )
    w.rows[(team_id, nba_date)] = snap
    return snap


def get_day(team=OWNER, target=YESTERDAY, provider_team_id=None):
    return asyncio.run(svc.LineupSnapshotService.get_day(team, target, provider_team_id))


# ---- shape ------------------------------------------------------------------------------


@pytest.mark.unit
def test_rows_become_a_snapshot_in_lineup_order():
    snap = svc.snapshot_from_rows(header(), [row(2, 12, "Benchwarmer"), row(1, 0, "Cade Cunningham", applied=48.0),
                                             row(3, 13, "Hurt Guy", pos=5, eligible=())], {1: 4432166})
    assert snap.source == "snapshot" and snap.captured_at == "2026-11-10T08:05:00+00:00"
    assert (snap.provider_league_id, snap.season, snap.provider_team_id, snap.nba_date) == ("552315826", 2027, 1, "2026-11-09")
    assert (snap.matchup_period_id, snap.opponent_provider_team_id, snap.applied_stat_total) == (4, 5, 145.0)
    assert [(p.name, p.lineup_slot, p.lineup_slot_id) for p in snap.players] == [
        ("Cade Cunningham", "PG", 0), ("Benchwarmer", "BE", 12), ("Hurt Guy", "IR", 13),
    ]
    cade = snap.players[0]
    assert cade.nba_player_id == 4432166 and cade.position == "PG" and cade.applied_total == 48.0
    assert cade.eligible_slots == ["PG", "G", "UT", "BE", "IR"]
    assert snap.players[2].position == "C" and snap.players[2].nba_player_id is None and snap.players[2].eligible_slots == []


# ---- get_day ------------------------------------------------------------------------------


@pytest.mark.unit
def test_stored_snapshot_is_returned_without_touching_espn(world):
    stored(world)
    resp = get_day()
    assert resp.data.source == "snapshot" and resp.data.provider_team_id == 1
    assert world.espn_calls == [] and world.hydrated == 0 and world.persisted == []


@pytest.mark.unit
def test_the_opponent_in_the_same_league_is_readable(world):
    stored(world, team_id=5, name="Rival")
    resp = get_day(provider_team_id=5)
    assert resp.data.team_name == "Rival" and resp.data.source == "snapshot"


@pytest.mark.unit
def test_a_missing_day_falls_back_to_espns_history(world):
    resp = get_day()
    assert world.espn_calls == [(("mTeam", "mRoster"), PERIOD)]
    assert resp.data.source == "provider_history" and resp.data.captured_at is None
    assert resp.data.scoring_period_id == PERIOD and resp.data.nba_date == "2026-11-09"
    assert [(p.name, p.lineup_slot, p.nba_player_id, p.injury_status) for p in resp.data.players] == [
        ("Starter 1", "PG", 9101, "ACTIVE"), ("Bench 1", "BE", 9201, "OUT"),
    ]
    assert world.persisted == []            # the id was known already


@pytest.mark.unit
def test_history_is_not_used_for_a_day_espn_has_not_finished(world):
    world.payload = espn_payload(latest=PERIOD)   # ESPN is still on that day
    with pytest.raises(NotFoundError) as err:
        get_day()
    assert err.value.error_code == svc.NOT_FOUND


@pytest.mark.unit
def test_a_foreign_team_id_is_not_found(world):
    stored(world)
    world.payload = espn_payload(teams=((1, "GloatingSoap369"),))
    with pytest.raises(NotFoundError):
        get_day(provider_team_id=77)


@pytest.mark.unit
def test_todays_and_future_days_are_refused(world):
    for target in (TODAY, TODAY.replace(day=20)):
        with pytest.raises(BadRequestError) as err:
            get_day(target=target)
        assert err.value.error_code == "DATE_NOT_PAST"


@pytest.mark.unit
def test_a_day_is_finished_once_espn_rolls_past_it_not_at_the_6_am_game_date(world, monkeypatch):
    monkeypatch.setattr(svc, "fantasy_today", matchup_days.fantasy_today)    # the real clock rule
    stored(world)
    with freeze_time("2026-11-10T08:00:00Z"):           # 3 AM ET: ESPN is on 11-10, the game date still 11-09
        assert get_day(target=YESTERDAY).data.source == "snapshot"
        data = asyncio.run(svc.LineupSnapshotService.list_range(OWNER, YESTERDAY, TODAY)).data
        assert [s.nba_date for s in data.snapshots] == ["2026-11-09"]
    with freeze_time("2026-11-10T06:30:00Z"):           # 1:30 AM ET: ESPN is still on 11-09
        with pytest.raises(BadRequestError) as err:
            get_day(target=YESTERDAY)
        assert err.value.error_code == "DATE_NOT_PAST"


@pytest.mark.unit
def test_yahoo_teams_have_no_snapshots(world):
    with pytest.raises(NotFoundError):
        get_day(team=YAHOO)


@pytest.mark.unit
def test_an_unknown_own_id_is_learned_from_the_days_rows(world):
    stored(world)
    resp = get_day(team=OWNER_NO_ID)
    assert resp.data.provider_team_id == 1 and world.persisted == [(7, 1)]


@pytest.mark.unit
def test_an_unknown_own_id_is_learned_from_espn_by_name(world):
    resp = get_day(team=OWNER_NO_ID)
    assert resp.data.source == "provider_history" and resp.data.provider_team_id == 1
    assert world.persisted == [(7, 1)]


# ---- list_range ------------------------------------------------------------------------------


@pytest.mark.unit
def test_range_lists_stored_days_and_names_the_missing_ones(world):
    stored(world, nba_date=date(2026, 11, 9))
    data = asyncio.run(svc.LineupSnapshotService.list_range(OWNER, None, None)).data
    assert (data.from_date, data.to_date, data.provider_team_id) == ("2026-11-09", "2026-11-15", 1)
    assert [s.nba_date for s in data.snapshots] == ["2026-11-09"]
    assert data.missing_dates == []                     # today and later days are not expected yet

    data = asyncio.run(svc.LineupSnapshotService.list_range(OWNER, date(2026, 11, 7), date(2026, 11, 9))).data
    assert data.missing_dates == ["2026-11-07", "2026-11-08"] and len(data.snapshots) == 1
    assert world.espn_calls == []                       # the list never reads ESPN


@pytest.mark.unit
def test_an_unknown_own_id_is_learned_from_any_stored_day_in_the_range(world):
    stored(world, nba_date=date(2026, 11, 7))           # the newest finished day has no row yet
    data = asyncio.run(svc.LineupSnapshotService.list_range(OWNER_NO_ID, date(2026, 11, 7), date(2026, 11, 9))).data
    assert data.provider_team_id == 1 and [s.nba_date for s in data.snapshots] == ["2026-11-07"]
    assert data.missing_dates == ["2026-11-08", "2026-11-09"]


@pytest.mark.unit
def test_range_limits(world):
    with pytest.raises(BadRequestError) as err:
        asyncio.run(svc.LineupSnapshotService.list_range(OWNER, date(2026, 10, 1), date(2026, 11, 9)))
    assert err.value.error_code == "DATE_RANGE_TOO_LONG"
    with pytest.raises(BadRequestError):
        asyncio.run(svc.LineupSnapshotService.list_range(OWNER, date(2026, 11, 9), date(2026, 11, 1)))


# ---- rosters_for_matchup ------------------------------------------------------------------------------


@pytest.mark.unit
def test_matchup_rosters_mix_stored_and_history_per_side(world):
    stored(world)                                        # only our side is stored
    league_info = svc.public_league_info(OWNER)
    found = asyncio.run(svc.LineupSnapshotService.rosters_for_matchup(league_info, {1: "GloatingSoap369", 5: "Rival"}, YESTERDAY))
    assert found[1].source == "snapshot" and found[5].source == "provider_history"
    assert world.espn_calls == [(("mTeam", "mRoster"), PERIOD)]


@pytest.mark.unit
def test_matchup_rosters_survive_a_failed_history_read(world, monkeypatch):
    async def boom(league_info, views, *, expect_key="teams", scoring_period_id=None):
        raise RuntimeError("espn down")

    monkeypatch.setattr(svc.EspnService, "fetch_league", staticmethod(boom))
    stored(world)
    league_info = svc.public_league_info(OWNER)
    found = asyncio.run(svc.LineupSnapshotService.rosters_for_matchup(league_info, {1: "GloatingSoap369", 5: "Rival"}, YESTERDAY))
    assert list(found) == [1]
