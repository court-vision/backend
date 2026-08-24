#!/usr/bin/env python
"""
Capture credential-stripped ESPN / Yahoo league payloads into tests/fixtures/.

For every connected team (or --team-id N) this fetches the provider's league
settings and the current matchup, removes credentials and personal fields, and
writes JSON fixtures named by the provider's scoring type. The settings parsers
are tested against these files, so provider shapes are verified before any
parser ships.

Yahoo: an expired access token is refreshed through the normal persisting path
(YahooService._ensure_valid_token with team_id), so run this against the
database that owns the team's tokens.

Usage:
    python scripts/capture_provider_fixtures.py [--team-id N] [--out tests/fixtures]
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.base import init_db  # noqa: E402
from db.models.teams import Team  # noqa: E402
from services.team_service import TeamService  # noqa: E402
from utils.constants import ESPN_FANTASY_ENDPOINT  # noqa: E402

DROP_KEYS = {
    "members", "owners", "primaryOwner", "managers", "manager", "guid", "email",
    "nickname", "image_url", "felo_score", "espn_s2", "swid", "SWID",
    "yahoo_access_token", "yahoo_refresh_token",
}


def scrub(obj):
    """Recursively drop credential / personal keys."""
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if k not in DROP_KEYS}
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    return obj


def write(out_dir: Path, name: str, payload) -> Path:
    path = out_dir / name
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    print(f"  wrote {path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path}")
    return path


def capture_espn(li, out_dir: Path) -> str:
    url = ESPN_FANTASY_ENDPOINT.format(li.year, li.league_id)
    resp = requests.get(
        url,
        params={"view": ["mSettings", "mTeam", "mMatchup", "mMatchupScore"]},
        cookies={"espn_s2": li.espn_s2, "SWID": li.swid},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()

    settings = data.get("settings", {})
    scoring_type = settings.get("scoringSettings", {}).get("scoringType", "UNKNOWN")
    slug = scoring_type.lower()

    write(out_dir, f"espn_settings_{slug}.json", {
        "id": data.get("id"),
        "seasonId": data.get("seasonId"),
        "status": data.get("status"),
        "settings": scrub(settings),
    })

    schedule = data.get("schedule", [])
    period = data.get("status", {}).get("currentMatchupPeriod")
    entries = [e for e in schedule if e.get("matchupPeriodId") == period] or schedule[-2:]
    write(out_dir, f"espn_matchup_{slug}.json", {
        "status": data.get("status"),
        "schedule": scrub(entries[:2]),
        "teams": [{"id": t.get("id"), "name": t.get("name"), "abbrev": t.get("abbrev")}
                  for t in data.get("teams", [])],
    })
    return scoring_type


async def capture_yahoo(team: Team, li, out_dir: Path) -> str:
    from services.yahoo_service import YahooService, YAHOO_API_BASE

    token = await YahooService._ensure_valid_token(li, team.team_id)
    league_key = li.yahoo_team_key.rsplit(".t.", 1)[0]
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    s = requests.get(f"{YAHOO_API_BASE}/league/{league_key}/settings?format=json", headers=headers, timeout=30)
    s.raise_for_status()
    settings_json = s.json()
    m = requests.get(f"{YAHOO_API_BASE}/team/{li.yahoo_team_key}/matchups?format=json", headers=headers, timeout=30)
    m.raise_for_status()

    scoring_type = "unknown"
    try:
        league = settings_json["fantasy_content"]["league"]
        meta = league[0] if isinstance(league, list) else league
        scoring_type = meta.get("scoring_type", scoring_type)
    except (KeyError, AttributeError, TypeError):
        pass

    write(out_dir, f"yahoo_settings_{scoring_type}.json", scrub(settings_json))
    write(out_dir, f"yahoo_matchups_{scoring_type}.json", scrub(m.json()))
    return scoring_type


async def main(team_id: int | None, out_dir: Path):
    init_db()
    out_dir.mkdir(parents=True, exist_ok=True)
    query = Team.select().order_by(Team.team_id)
    if team_id:
        query = query.where(Team.team_id == team_id)

    seen: set[tuple[str, str]] = set()
    for team in query:
        li = TeamService.deserialize_league_info(json.loads(team.league_info))
        key = (li.provider.value, str(li.league_id))
        if key in seen:
            continue
        seen.add(key)
        print(f"team {team.team_id}: {li.provider.value} league {li.league_id} ({li.year})")
        try:
            if li.provider.value == "yahoo":
                st = await capture_yahoo(team, li, out_dir)
            else:
                st = capture_espn(li, out_dir)
            print(f"  scoring type: {st}")
        except Exception as exc:  # keep going; report at the end
            print(f"  FAILED: {exc}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--team-id", type=int)
    ap.add_argument("--out", default=str(Path(__file__).parent.parent / "tests" / "fixtures"))
    args = ap.parse_args()
    asyncio.run(main(args.team_id, Path(args.out)))
