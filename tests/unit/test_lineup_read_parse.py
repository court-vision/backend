"""
services.lineup_read_service.parse_espn_lineup: the pure half of the lineup read.

Payloads are built to ESPN's mTeam + mRoster + mSettings shape (the same keys the
matchup and settings fixtures carry), so the parser is exercised on: team
resolution by id vs name, owner check via `owners[]`, slot counts from
`rosterSettings.lineupSlotCounts` (and the name-keyed fallback), per-entry
eligibility / lock / injury fields, and the scoring period.
"""

import json
from pathlib import Path

import pytest

from core.errors import BadRequestError
from services.lineup_read_service import (
    ParsedLineup,
    parse_espn_lineup,
    roster_version,
    slot_counts_from_names,
    slot_rows,
)
from schemas.lineup_editor import LineupPlayer

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SETTINGS = json.loads((FIXTURES / "espn_settings_h2h_category.json").read_text())
COUNTS = SETTINGS["settings"]["rosterSettings"]["lineupSlotCounts"]
SWID = "{ABCDEF01-2345-6789-ABCD-EF0123456789}"


def entry(pid, slot, eligible, *, locked=False, injured=False, status="ACTIVE", team=7, name=None):
    return {
        "playerId": pid, "lineupSlotId": slot, "injuryStatus": status,
        "playerPoolEntry": {
            "id": pid, "lineupLocked": locked, "onTeamId": 4,
            "player": {"id": pid, "fullName": name or f"Player {pid}", "proTeamId": team,
                       "eligibleSlots": list(eligible), "injured": injured, "injuryStatus": status,
                       "defaultPositionId": 1},
        },
    }


def payload(*, team_id=4, team_name="Lvl. 3 Goblins", owners=(SWID,), entries=None, period=12, counts=COUNTS,
            other_team=True):
    teams = [{"id": team_id, "name": team_name, "owners": list(owners) if owners is not None else None,
              "roster": {"entries": entries if entries is not None else [
                  entry(1, 0, [0, 5, 11, 12]), entry(2, 11, [1, 5, 11, 12], locked=True),
                  entry(3, 12, [4, 9, 10, 11, 12], injured=True, status="OUT", team=3),
                  entry(4, 13, [2, 6, 11, 12, 13], injured=True, status="OUT"),
              ]}}]
    if other_team:
        teams.append({"id": 9, "name": "Only Franz", "owners": ["{OTHER}"], "roster": {"entries": []}})
    body = {"teams": teams, "status": {"latestScoringPeriod": period}}
    if period is not None:
        body["scoringPeriodId"] = period
    if counts is not None:
        body["settings"] = {"rosterSettings": {"lineupSlotCounts": counts, "lineupLocktimeType": "INDIVIDUAL_GAME"}}
    return body


@pytest.mark.unit
def test_parses_slots_eligibility_locks_and_injuries():
    parsed = parse_espn_lineup(payload(), team_name="Lvl. 3 Goblins", espn_team_id=None, swid=SWID)
    assert isinstance(parsed, ParsedLineup)
    assert parsed.espn_team_id == 4 and parsed.resolved_by == "name"
    assert parsed.scoring_period_id == 12 and parsed.lock_type == "INDIVIDUAL_GAME"
    assert parsed.slot_counts[11] == 3 and parsed.slot_counts[12] == 3 and parsed.slot_counts[13] == 1
    by_id = {e.player_id: e for e in parsed.entries}
    assert by_id[1].lineup_slot_id == 0 and by_id[1].eligible_slot_ids == (0, 5, 11, 12)
    assert by_id[2].lineup_locked is True
    assert by_id[3].injured and by_id[3].injury_status == "OUT" and by_id[3].pro_team == "NOP"
    assert by_id[4].lineup_slot_id == 13 and 13 in by_id[4].eligible_slot_ids
    assert by_id[1].pro_team == "DEN"


@pytest.mark.unit
def test_resolves_by_espn_team_id_before_name():
    parsed = parse_espn_lineup(payload(team_name="Renamed Team"), team_name="Lvl. 3 Goblins", espn_team_id=4, swid=SWID)
    assert parsed.resolved_by == "id" and parsed.team_name == "Renamed Team"


@pytest.mark.unit
def test_unknown_team_raises_the_existing_code():
    with pytest.raises(BadRequestError) as exc:
        parse_espn_lineup(payload(), team_name="Nope", espn_team_id=77)
    assert exc.value.error_code == "TEAM_NAME_NOT_IN_LEAGUE"


@pytest.mark.unit
@pytest.mark.parametrize("swid, owners, expected", [
    (SWID, (SWID,), "ok"),
    (SWID.lower().strip("{}"), (SWID,), "ok"),          # braces and case are normalized
    ("{SOMEONE-ELSE}", (SWID,), "not_owner"),
    (SWID, None, "unknown"),                             # ESPN omitted owners[]
    (None, (SWID,), "not_owner"),                        # no cookie => cannot be the owner
])
def test_owner_check(swid, owners, expected):
    parsed = parse_espn_lineup(payload(owners=owners), team_name="Lvl. 3 Goblins", swid=swid)
    assert parsed.owner_check == expected


@pytest.mark.unit
def test_slot_counts_fall_back_to_the_synced_league_when_settings_are_missing():
    parsed = parse_espn_lineup(payload(counts=None), team_name="Lvl. 3 Goblins",
                               fallback_slot_counts={"PG": 1, "UT": 3, "BE": 3, "IR": 1, "Bogus": 9})
    assert parsed.slot_counts == {0: 1, 11: 3, 12: 3, 13: 1}
    assert slot_counts_from_names(None) == {}


@pytest.mark.unit
def test_scoring_period_falls_back_to_status_then_none():
    body = payload(period=None)
    assert parse_espn_lineup(body, team_name="Lvl. 3 Goblins").scoring_period_id is None
    body["status"] = {"latestScoringPeriod": 3}
    assert parse_espn_lineup(body, team_name="Lvl. 3 Goblins").scoring_period_id == 3


@pytest.mark.unit
def test_slot_rows_follow_render_order_and_skip_unused_slots():
    rows = slot_rows({int(k): v for k, v in COUNTS.items()})
    assert [(r.slot, r.count) for r in rows] == [("PG", 1), ("SG", 1), ("SF", 1), ("PF", 1), ("C", 1), ("G", 1),
                                                 ("F", 1), ("UT", 3), ("BE", 3), ("IR", 1)]


def _player(pid, slot):
    return LineupPlayer(player_id=pid, name=str(pid), team="DEN", lineup_slot_id=slot, lineup_slot="X",
                        eligible_slot_ids=[slot], eligible_slots=["X"])


@pytest.mark.unit
def test_roster_version_tracks_period_and_assignment_only():
    a = roster_version(12, [_player(1, 0), _player(2, 12)])
    assert a == roster_version(12, [_player(2, 12), _player(1, 0)])     # order-independent
    assert a != roster_version(13, [_player(1, 0), _player(2, 12)])     # new ESPN day
    assert a != roster_version(12, [_player(1, 12), _player(2, 0)])     # a swap
    assert len(a) == 16
