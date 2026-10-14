"""Which team in an ESPN league payload is the user's.

The team name is the identity a user types when adding a team, but it is the
one thing they can change on ESPN at any time. So a stored team is found by,
in order: ESPN's numeric team id (once learned), the stored name, then the one
team the user's SWID owns. The last rule is what keeps a renamed team working
before its id has been learned; once found, the caller records the id and the
new name so the next lookup is by id.
"""

import json
from typing import Iterable, Optional

from services.credential_service import normalize_swid


def find_espn_team(
    teams: Iterable[dict],
    *,
    team_name: Optional[str],
    espn_team_id: Optional[int] = None,
    swid: Optional[str] = None,
) -> Optional[dict]:
    teams = list(teams or [])
    if espn_team_id is not None:
        found = next((t for t in teams if t.get("id") == espn_team_id), None)
        if found is not None:
            return found
    wanted = (team_name or "").strip()
    if wanted:
        found = next((t for t in teams if (t.get("name") or "").strip() == wanted), None)
        if found is not None:
            return found
    mine = normalize_swid(swid)
    if mine:
        owned = [
            t for t in teams
            if mine in {normalize_swid(o) for o in (t.get("owners") or []) if isinstance(o, str)}
        ]
        if len(owned) == 1:
            return owned[0]
    return None


def persist_espn_identity(team_id: int, espn_team_id: Optional[int], team_name: Optional[str]) -> bool:
    """Record ESPN's team id and current name on the STORED json (never the hydrated copy).

    The name also keys `team_identifier`, which is how adding the same team
    again is recognised, so it moves with the name. Returns whether anything
    changed.
    """
    from db.models.teams import Team

    team = Team.get_or_none(Team.team_id == team_id)
    if team is None:
        return False
    stored = json.loads(team.league_info)
    before = dict(stored)
    fields = [Team.league_info]
    if espn_team_id is not None:
        stored["espn_team_id"] = espn_team_id
    name = (team_name or "").strip()
    if name and stored.get("team_name") != name:
        stored["team_name"] = name
        team.team_identifier = str(stored.get("league_id", "")) + name
        fields.append(Team.team_identifier)
    if stored == before:
        return False
    team.league_info = json.dumps(stored)
    team.save(only=fields)
    return True
