"""
Lineup snapshot reads against real SQL: rows in migration 0029's tables, as
the nightly pipeline writes them, read back by `LineupSnapshotService`.
"""

import asyncio
import json
from datetime import date
from types import SimpleNamespace

import pytest
from freezegun import freeze_time

from db.models.lineup_snapshots import LineupSnapshot, LineupSnapshotPlayer
from services.lineup_snapshot_service import LineupSnapshotService

pytestmark = pytest.mark.integration

OPENING_NIGHT = date(2026, 10, 20)
# A saved team whose ESPN team id has not been learned yet.
OWNER_NO_ID = SimpleNamespace(
    team_id=7, user_id=42, league_id=None, league=None,
    league_info_json=json.dumps({"provider": "espn", "league_id": 1001, "team_name": "Own Team", "year": 2027}),
)


def _stored(team_id: int, team_name: str, day: date, league_id: str = "1001") -> None:
    header = LineupSnapshot.create(
        provider="espn", provider_league_id=league_id, season=2027, provider_team_id=team_id,
        team_name=team_name, scoring_period_id=(day - OPENING_NIGHT).days + 1, nba_date=day, player_count=1,
    )
    LineupSnapshotPlayer.create(snapshot=header, player_id=100 + team_id, player_name=f"Starter {team_id}",
                                pro_team="DET", default_position_id=1, lineup_slot_id=0)


@freeze_time("2026-11-10T17:00:00Z")  # noon ET
def test_an_unknown_own_id_is_learned_from_any_stored_day_in_the_range(integration_db):
    for day in (date(2026, 11, 7), date(2026, 11, 8)):           # the pipeline has not written 11-09 yet
        _stored(1, "Own Team", day)
        _stored(5, "Rival", day)
    _stored(3, "Own Team", date(2026, 11, 9), league_id="2002")  # the same name in another league

    data = asyncio.run(LineupSnapshotService.list_range(OWNER_NO_ID, date(2026, 11, 7), date(2026, 11, 9))).data
    assert data.provider_team_id == 1
    assert [(s.provider_team_id, s.nba_date) for s in data.snapshots] == [(1, "2026-11-07"), (1, "2026-11-08")]
    assert data.missing_dates == ["2026-11-09"]
