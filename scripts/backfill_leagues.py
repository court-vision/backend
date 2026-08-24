#!/usr/bin/env python
"""
Backfill usr.leagues for existing teams by syncing each team's provider settings.

Usage:
    python scripts/backfill_leagues.py --dry-run          # list teams and what would be synced
    python scripts/backfill_leagues.py                    # sync teams with no league yet
    python scripts/backfill_leagues.py --force            # re-sync every team
    python scripts/backfill_leagues.py --team-id 17
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.base import init_db  # noqa: E402
from db.models.teams import Team  # noqa: E402
from services.league_service import LeagueService  # noqa: E402
from services.team_service import TeamService  # noqa: E402


async def main(dry_run: bool, force: bool, team_id: int | None):
    init_db()
    query = Team.select().order_by(Team.team_id)
    if team_id:
        query = query.where(Team.team_id == team_id)
    for team in query:
        li = TeamService.deserialize_league_info(json.loads(team.league_info))
        provider, pid, season = LeagueService.provider_league_key(li)
        state = f"league_id={team.league_id}" if team.league_id else "unlinked"
        print(f"team {team.team_id:>3} | {provider:<5} {pid:<18} {season} | {state}")
        if dry_run or (team.league_id and not force):
            continue
        league = await LeagueService.sync_for_team(team, li)
        if league is None:
            print("      -> sync FAILED")
            continue
        sync = (league.raw_settings or {}).get("_sync", {})
        print(f"      -> league {league.id}: {league.scoring_type}"
              f"{' / ' + league.category_win_mode if league.category_win_mode else ''}"
              f" | synced={league.settings_synced_at is not None}"
              f" | weights={league.point_weights if league.scoring_type == 'points' else '-'}"
              f" | categories={[c['key'] for c in league.categories] if league.categories else '-'}"
              f" | unsupported={sync.get('unsupported', [])}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--team-id", type=int)
    a = ap.parse_args()
    asyncio.run(main(a.dry_run, a.force, a.team_id))
