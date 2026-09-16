#!/usr/bin/env python
"""
Capture credential-stripped ESPN / Yahoo league payloads into tests/fixtures/.

For every connected team (or --team-id N) this fetches the provider's league
settings and the current matchup, removes credentials and personal fields, and
writes JSON fixtures named by the provider's scoring type. The settings parsers
are tested against these files, so provider shapes are verified before any
parser ships. Yahoo also gets the roster on a date, a free-agent page, the
account's teams, the scoreboard, draft results and per-day player stats --
the inputs of the Yahoo parity work (docs/YAHOO_PARITY_PLAN.md, P0).

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
    "yahoo_access_token", "yahoo_refresh_token", "xoauth_yahoo_guid", "profile_url",
    # A draft pick's `memberId` is the SWID of whoever clicked it — ESPN sets it
    # on human picks only, so it is easy to miss in a payload that is mostly
    # autopicks.
    "memberId",
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


def _yahoo_get(url: str, headers: dict):
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json()


async def capture_yahoo(team: Team, li, out_dir: Path) -> str:
    """Settings and matchups (the parsers' inputs), then everything the Yahoo
    parity work needs a real shape for: the roster on a date, a free-agent
    page, the account's teams, the league scoreboard, draft results and
    per-day player stats. Each extra call is best-effort -- draft results do
    not exist before the draft, a scoreboard has no week in the offseason --
    so one refusal never costs the others."""
    from datetime import date

    from services.yahoo_service import YahooService, YAHOO_API_BASE

    token = await YahooService._ensure_valid_token(li, team.team_id)
    team_key = li.yahoo_team_key
    league_key = team_key.rsplit(".t.", 1)[0]
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    settings_json = _yahoo_get(f"{YAHOO_API_BASE}/league/{league_key}/settings?format=json", headers)
    matchups_json = _yahoo_get(f"{YAHOO_API_BASE}/team/{team_key}/matchups?format=json", headers)

    scoring_type = "unknown"
    try:
        league = settings_json["fantasy_content"]["league"]
        meta = league[0] if isinstance(league, list) else league
        scoring_type = meta.get("scoring_type", scoring_type)
    except (KeyError, AttributeError, TypeError):
        pass

    write(out_dir, f"yahoo_settings_{scoring_type}.json", scrub(settings_json))
    write(out_dir, f"yahoo_matchups_{scoring_type}.json", scrub(matchups_json))

    today = date.today().isoformat()
    extras = {
        f"yahoo_roster_{scoring_type}.json":
            f"{YAHOO_API_BASE}/team/{team_key}/roster;date={today}/players?format=json",
        f"yahoo_free_agents_{scoring_type}.json":
            f"{YAHOO_API_BASE}/league/{league_key}/players;status=FA;sort=OR;start=0;count=25/percent_owned?format=json",
        "yahoo_account_teams.json":
            f"{YAHOO_API_BASE}/users;use_login=1/games;game_codes=nba/teams?format=json",
        f"yahoo_scoreboard_{scoring_type}.json":
            f"{YAHOO_API_BASE}/league/{league_key}/scoreboard?format=json",
        f"yahoo_draftresults_{scoring_type}.json":
            f"{YAHOO_API_BASE}/league/{league_key}/draftresults?format=json",
    }
    captured: dict[str, dict] = {}
    for name, url in extras.items():
        try:
            captured[name] = _yahoo_get(url, headers)
            write(out_dir, name, scrub(captured[name]))
        except Exception as exc:  # best-effort: report and keep going
            print(f"  skipped {name}: {exc}")

    # Per-day stats for the first few rostered players, keyed the way the
    # lineup board and the daily matchup view will ask for them.
    player_keys = _yahoo_player_keys(captured.get(f"yahoo_roster_{scoring_type}.json"))[:3]
    if player_keys:
        try:
            keys = ",".join(player_keys)
            stats = _yahoo_get(
                f"{YAHOO_API_BASE}/league/{league_key}/players;player_keys={keys}/stats;type=date;date={today}?format=json",
                headers,
            )
            write(out_dir, f"yahoo_player_stats_date_{scoring_type}.json", scrub(stats))
        except Exception as exc:
            print(f"  skipped yahoo_player_stats_date_{scoring_type}.json: {exc}")
    return scoring_type


def _yahoo_player_keys(roster_json) -> list[str]:
    """Every `player_key` in a Yahoo roster payload, in order. Yahoo nests
    players as {"0": {"player": [[{...}, {...}], ...]}, "count": n}."""
    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            key = node.get("player_key")
            if isinstance(key, str) and key not in found:
                found.append(key)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(roster_json)
    return found


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
