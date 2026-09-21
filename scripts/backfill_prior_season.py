"""
Bring the 2024-25 season into `nba.player_season_stats` from the legacy
`stats_s1` schema, so the draft board's walk-back has a season to walk back to.

    .venv/bin/python scripts/backfill_prior_season.py --dry-run
    .venv/bin/python scripts/backfill_prior_season.py
    .venv/bin/python scripts/backfill_prior_season.py --from-csv s1_2024_25.csv

Why this exists: `services.scoring.pool.baseline_records(walk_back=True)` values
a player off the most recent season he actually played, which is what stops a
missed season erasing him from the draft board. It reads `nba.*` and nothing
else, so the season before the migration has to live there too.

`stats_s1.daily_stats` is per-game rows from before the `nba.*` schema existed
(2024-10-22 to 2025-04-13, 569 players). Its `id` is nba_api's PLAYER_ID — the
same id space as `nba.players.id` — because the ETL that wrote it read
`leagueleaders.LeagueLeaders(season='2024-25')` keyed on `player['PLAYER_ID']`.
So it joins directly, with no mapping.

`fpts` is recomputed from the raw columns rather than carried over: the legacy
value came from a 2025-era formula that is not guaranteed to match the platform
default, and every `fpts` column in `nba.*` was written with that default.

`--from-csv` takes the same data as an export (id, gp, min, and the counting
stats, one row per player) for an environment where the legacy schema is not
present — the dev database has `stats_s2` but no `stats_s1`, so this is the only
way to exercise the path there.

Idempotent: one row per player at the season's last date, matching the
(player_id, as_of_date) unique index, re-runnable without duplicating.
"""

import argparse
import csv
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

os.environ.setdefault("CLERK_JWKS_URL", "https://fake.clerk.dev/.well-known/jwks.json")
os.environ.setdefault("CLERK_SECRET_KEY", "sk_test_fake")
os.environ.setdefault("PIPELINE_API_TOKEN", "backfill-only")

from db.base import db  # noqa: E402
from db.models.nba.player_season_stats import PlayerSeasonStats  # noqa: E402
from db.models.nba.players import Player  # noqa: E402
from db.models.nba.teams import NBATeam  # noqa: E402
from services.scoring.models import StatLine  # noqa: E402
from services.scoring.points import DEFAULT_POINTS  # noqa: E402

SEASON = "2024-25"
# The season's last game. Every row lands here so `distinct on (player)
# order by as_of_date desc` picks it, and a re-run overwrites rather than adds.
AS_OF = date(2025, 4, 13)

SUM_COLUMNS = ("min", "pts", "reb", "ast", "stl", "blk", "tov",
               "fgm", "fga", "fg3m", "fg3a", "ftm", "fta")


def from_legacy_schema() -> list[dict]:
    """Season totals per player, aggregated from stats_s1.daily_stats."""
    sums = ", ".join(f"sum({c}) as {c}" for c in SUM_COLUMNS)
    cursor = db.execute_sql(
        f"select id, count(*) as gp, {sums}, max(date) as last_date "
        f"from stats_s1.daily_stats group by id"
    )
    columns = [c[0] for c in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def from_csv(path: str) -> list[dict]:
    """The same shape from an export — see the module docstring."""
    with open(path) as handle:
        return [{k: (int(v) if v not in ("", None) else 0) for k, v in row.items()}
                for row in csv.DictReader(handle)]


def latest_teams() -> dict[int, str]:
    """Each player's last 2024-25 team, where the legacy schema can say.

    `team_id` is a restricted foreign key, so a team abbreviation that is not in
    `nba.teams` (a relocation, or a legacy spelling) has to become NULL rather
    than fail the whole backfill for one row.
    """
    known = {t.id for t in NBATeam.select(NBATeam.id)}
    cursor = db.execute_sql(
        "select distinct on (id) id, team from stats_s1.daily_stats order by id, date desc"
    )
    return {pid: team for pid, team in cursor.fetchall() if team in known}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="report what would be written")
    parser.add_argument("--from-csv", metavar="PATH", help="read an export instead of stats_s1")
    args = parser.parse_args()

    db.connect(reuse_if_open=True)

    rows = from_csv(args.from_csv) if args.from_csv else from_legacy_schema()
    teams = {} if args.from_csv else latest_teams()
    known_players = {p.id for p in Player.select(Player.id)}

    written = skipped_unknown = skipped_empty = 0
    existing = {
        r.player_id for r in
        PlayerSeasonStats.select(PlayerSeasonStats.player).where(PlayerSeasonStats.season == SEASON)
    }

    with db.atomic():
        for row in rows:
            pid, gp = int(row["id"]), int(row["gp"])
            if pid not in known_players:
                # Left the league before nba.players was populated from current
                # rosters. Nothing to project for, and the FK would refuse it.
                skipped_unknown += 1
                continue
            if gp < 1:
                skipped_empty += 1
                continue

            totals = {c: int(row[c] or 0) for c in SUM_COLUMNS}
            fpts = round(DEFAULT_POINTS.score(
                StatLine.from_dict({k: v / gp for k, v in totals.items()})
            ) * gp)

            if args.dry_run:
                written += 1
                continue

            values = {**totals, "gp": gp, "fpts": fpts, "team": teams.get(pid)}
            record, created = PlayerSeasonStats.get_or_create(
                player_id=pid, as_of_date=AS_OF, defaults={**values, "season": SEASON}
            )
            if not created:
                for key, value in {**values, "season": SEASON}.items():
                    setattr(record, key, value)
                record.save()
            written += 1

    verb = "would write" if args.dry_run else "wrote"
    print(f"{verb} {written} {SEASON} rows "
          f"({len(existing)} already present before this run)")
    print(f"skipped {skipped_unknown} players absent from nba.players, "
          f"{skipped_empty} with no games")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
