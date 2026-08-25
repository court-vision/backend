#!/usr/bin/env python
"""
Rehearse the lineup service end to end against a running features instance.

Builds the request exactly the way `POST /v1/internal/lineups/generate` does
(roster + free agents from the team's stored provider credentials, `avg_points`
from our stored stats under the team's scoring), posts it to the features
service for each requested week, and checks the plan that comes back: one
lineup day per calendar day of the week, a non-negative improvement, no
negative values. A week outside the calendar is expected to come back as the
service's own rejection (an ERROR response, not a 500).

Usage:
    python scripts/rehearse_lineup.py --team-id 15 --weeks 1,18,24
    python scripts/rehearse_lineup.py --team-id 15 --weeks 1,18,24,99 --features-url http://localhost:8080
    python scripts/rehearse_lineup.py --team-id 15 --weeks 18 --preview categories
    python scripts/rehearse_lineup.py --team-id 15 --weeks 1 --save     # persist like PUT /lineups/save

Reads the team and stats from DATABASE_URL (no migrations are applied); only
--save writes, one usr.lineups row per successful week. NBA_SEASON / ESPN_YEAR
come from the environment exactly as they do for the API. Exit code 1 if any
week fails its checks.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.logging import setup_logging  # noqa: E402
from core.settings import settings  # noqa: E402
from db.base import db  # noqa: E402
from db.models.teams import Team  # noqa: E402
from schemas.common import ApiStatus  # noqa: E402
from schemas.lineup import LineupInfo  # noqa: E402
from services import features_client  # noqa: E402
from services.lineup_service import LineupService  # noqa: E402
from services.player_value_service import PlayerValueService  # noqa: E402
from services.schedule_service import get_matchup_by_number, get_max_week  # noqa: E402
from services.team_service import TeamService  # noqa: E402


def _all_values(lineup: LineupInfo) -> list[float]:
    values: list[float] = []
    for gene in lineup.Lineup:
        values += [p.AvgPoints for p in gene.Additions]
        values += [p.AvgPoints for p in gene.Removals]
        values += [p.AvgPoints for p in gene.Roster.values()]
    return values


async def rehearse_week(week: int, roster, fas, slots: int, save: bool, user_id: int, team_id: int) -> bool:
    calendar_week = get_matchup_by_number(week)
    game_span = calendar_week["game_span"] if calendar_week else None

    started = time.perf_counter()
    resp = await LineupService.generate_lineup_v2(roster, fas, slots, week)
    elapsed = time.perf_counter() - started

    if resp.status != ApiStatus.SUCCESS:
        expected = calendar_week is None   # only a week outside the calendar may be rejected
        verdict = "PASS (expected rejection)" if expected else "FAIL"
        print(f"week {week:>2} | game_span={game_span} | status={resp.status} error_code={resp.error_code} "
              f"message={resp.message!r} | {elapsed:.1f}s | {verdict}")
        return expected

    lineup = resp.data
    values = _all_values(lineup)
    adds = sum(len(g.Additions) for g in lineup.Lineup)
    drops = sum(len(g.Removals) for g in lineup.Lineup)
    checks = {
        "Improvement>=0": lineup.Improvement >= 0,
        "len(Lineup)==game_span": len(lineup.Lineup) == game_span,
        "AvgPoints>=0": all(v >= 0 for v in values),
    }
    failed = [name for name, ok in checks.items() if not ok]
    print(f"week {week:>2} | game_span={game_span} | len(Lineup)={len(lineup.Lineup)} | Improvement={lineup.Improvement} "
          f"| AvgPoints min={min(values):.1f} max={max(values):.1f} | adds={adds} drops={drops} "
          f"| {elapsed:.1f}s | {'PASS' if not failed else 'FAIL ' + ','.join(failed)}")

    if save:
        saved = await LineupService.save_lineup(user_id, team_id, lineup)
        print(f"         save -> {saved.status}: {saved.message}")
    return not failed


async def main(args: argparse.Namespace) -> int:
    setup_logging(settings.log_level, json_format=True, service_name="rehearse-lineup")
    logging.getLogger("httpx").setLevel(logging.WARNING)   # our structured line per call is enough
    if args.features_url:
        features_client.FEATURES_URL = args.features_url

    db.connect()
    try:
        row = Team.select(Team.team_id, Team.user_id, Team.league_info).where(Team.team_id == args.team_id).dicts().get()
        user_id, team_id = int(row["user_id"]), int(row["team_id"])
        league_info = TeamService.deserialize_league_info(json.loads(row["league_info"]))
        if args.preview:
            league_info.scoring_preview = args.preview
        scoring = PlayerValueService.scoring_for(league_info, None if args.preview else team_id)

        print(f"team {team_id} {league_info.team_name!r} (user {user_id}) | provider={league_info.provider.value} "
              f"league={league_info.league_id} year={league_info.year} | scoring={scoring.format} "
              f"preview={args.preview or league_info.scoring_preview} | avg_mode={args.avg_mode} streaming_slots={args.streaming_slots}")
        print(f"backend: season={settings.nba_season} espn_year={settings.espn_year} calendar_weeks={get_max_week()}")

        try:
            health = await features_client.get_healthz()
            print(f"features: {features_client.FEATURES_URL} status={health.get('status')} "
                  f"schedule_file={health.get('schedule_file')} weeks={health.get('weeks')}")
        except httpx.HTTPError as exc:   # keep going: the generate call shows the mapped failure
            print(f"features: {features_client.FEATURES_URL} healthz unreachable ({type(exc).__name__}: {exc})")

        started = time.perf_counter()
        roster, fas = await LineupService.fetch_roster_and_fas(
            user_id, team_id, use_recent_stats=(args.avg_mode == "recent"), scoring_preview=args.preview,
        )
        players = roster + fas
        values = [p.avg_points for p in players]
        print(f"roster={len(roster)} free_agents={len(fas)} | value_kind={dict(Counter(p.value_kind for p in players))} "
              f"| value_source={dict(Counter(p.value_source for p in players))} "
              f"| avg_points min={min(values):.1f} max={max(values):.1f} | fetched in {time.perf_counter() - started:.1f}s")

        results = []
        for week in args.weeks:
            results.append(await rehearse_week(week, roster, fas, args.streaming_slots, args.save, user_id, team_id))
    finally:
        if not db.is_closed():
            db.close()

    print(f"{sum(results)}/{len(results)} weeks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--team-id", type=int, required=True)
    ap.add_argument("--weeks", type=lambda s: [int(w) for w in s.split(",") if w.strip()], default=[1])
    ap.add_argument("--features-url", help="features service base URL (default: FEATURES_SERVER_ENDPOINT)")
    ap.add_argument("--preview", choices=["points", "categories"], help="render the team under this scoring format")
    ap.add_argument("--avg-mode", choices=["season", "recent"], default="season")
    ap.add_argument("--streaming-slots", type=int, default=2)
    ap.add_argument("--save", action="store_true", help="persist each generated lineup via LineupService.save_lineup")
    sys.exit(asyncio.run(main(ap.parse_args())))
