#!/usr/bin/env python
"""
Migration Script: stats_s2 -> nba schema

Backfills data from the legacy stats_s2 schema to the new normalized nba schema.

Usage:
    # Dry run (no changes)
    python scripts/migrate_to_nba_schema.py --dry-run

    # Full migration
    python scripts/migrate_to_nba_schema.py

    # Migrate specific tables
    python scripts/migrate_to_nba_schema.py --tables players,game_stats
"""

import argparse
import sys
from datetime import datetime
from itertools import islice
from pathlib import Path

# Add backend to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from db.base import db, init_db
from db.models.stats.daily_player_stats import DailyPlayerStats
from db.models.stats.cumulative_player_stats import CumulativePlayerStats
from db.models.nba import (
    Player,
    NBATeam,
    PlayerGameStats,
    PlayerSeasonStats,
    PlayerOwnership,
)


def batched(iterable, n):
    """Batch an iterable into chunks of size n."""
    it = iter(iterable)
    while batch := list(islice(it, n)):
        yield batch


class MigrationRunner:
    """Handles migration from stats_s2 to nba schema."""

    def __init__(self, dry_run: bool = False, batch_size: int = 1000):
        self.dry_run = dry_run
        self.batch_size = batch_size
        self.stats = {
            "teams": 0,
            "players": 0,
            "game_stats": 0,
            "season_stats": 0,
            "ownership": 0,
        }

    def log(self, message: str):
        """Print with timestamp."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        prefix = "[DRY RUN] " if self.dry_run else ""
        print(f"[{timestamp}] {prefix}{message}")

    def migrate_teams(self):
        """Seed NBA teams dimension table."""
        self.log("Migrating teams...")

        if self.dry_run:
            self.stats["teams"] = 30
            self.log(f"Would insert 30 NBA teams")
            return

        count = NBATeam.seed_teams()
        self.stats["teams"] = count
        self.log(f"Inserted {count} NBA teams")

    def migrate_players(self):
        """Extract unique players from daily stats into players dimension."""
        self.log("Migrating players...")

        # Get unique players from daily_player_stats
        query = (
            DailyPlayerStats.select(
                DailyPlayerStats.id,
                DailyPlayerStats.espn_id,
                DailyPlayerStats.name,
                DailyPlayerStats.name_normalized,
            )
            .distinct()
            .order_by(DailyPlayerStats.id)
        )

        count = 0
        for row in query:
            if self.dry_run:
                count += 1
                continue

            Player.upsert_player(
                player_id=row.id,
                name=row.name,
                espn_id=row.espn_id,
            )
            count += 1

            if count % 100 == 0:
                self.log(f"  Processed {count} players...")

        self.stats["players"] = count
        self.log(f"Migrated {count} players")

    def migrate_game_stats(self):
        """Migrate daily_player_stats to player_game_stats."""
        self.log("Migrating game stats...")

        # Count total records
        total = DailyPlayerStats.select().count()
        self.log(f"Found {total} game stat records to migrate")

        if self.dry_run:
            self.stats["game_stats"] = total
            self.log(f"Would migrate {total} game stat records")
            return

        query = DailyPlayerStats.select().order_by(
            DailyPlayerStats.date, DailyPlayerStats.id
        )

        count = 0
        for batch in batched(query.iterator(), self.batch_size):
            records = []
            for row in batch:
                records.append({
                    "player_id": row.id,
                    "team_id": row.team if len(row.team) <= 3 else None,
                    "game_date": row.date,
                    "fpts": row.fpts,
                    "pts": row.pts,
                    "reb": row.reb,
                    "ast": row.ast,
                    "stl": row.stl,
                    "blk": row.blk,
                    "tov": row.tov,
                    "min": row.min,
                    "fgm": row.fgm,
                    "fga": row.fga,
                    "fg3m": row.fg3m,
                    "fg3a": row.fg3a,
                    "ftm": row.ftm,
                    "fta": row.fta,
                    "pipeline_run_id": row.pipeline_run_id,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                })

            # Batch insert with conflict handling
            PlayerGameStats.insert_many(records).on_conflict(
                conflict_target=[PlayerGameStats.player, PlayerGameStats.game_date],
                preserve=[
                    PlayerGameStats.fpts,
                    PlayerGameStats.pts,
                    PlayerGameStats.reb,
                    PlayerGameStats.ast,
                    PlayerGameStats.stl,
                    PlayerGameStats.blk,
                    PlayerGameStats.tov,
                    PlayerGameStats.min,
                    PlayerGameStats.fgm,
                    PlayerGameStats.fga,
                    PlayerGameStats.fg3m,
                    PlayerGameStats.fg3a,
                    PlayerGameStats.ftm,
                    PlayerGameStats.fta,
                ],
            ).execute()

            count += len(records)
            if count % 5000 == 0:
                self.log(f"  Migrated {count}/{total} game stats...")

        self.stats["game_stats"] = count
        self.log(f"Migrated {count} game stat records")

    def migrate_season_stats(self):
        """Migrate cumulative_player_stats to player_season_stats."""
        self.log("Migrating season stats...")

        # Count total records
        total = CumulativePlayerStats.select().count()
        self.log(f"Found {total} season stat records to migrate")

        if self.dry_run:
            self.stats["season_stats"] = total
            self.log(f"Would migrate {total} season stat records")
            return

        query = CumulativePlayerStats.select().order_by(
            CumulativePlayerStats.date, CumulativePlayerStats.id
        )

        count = 0
        for batch in batched(query.iterator(), self.batch_size):
            records = []
            for row in batch:
                # Determine season from date
                year = row.date.year
                month = row.date.month
                if month >= 8:
                    season = f"{year}-{str(year + 1)[-2:]}"
                else:
                    season = f"{year - 1}-{str(year)[-2:]}"

                records.append({
                    "player_id": row.id,
                    "team_id": row.team if len(row.team) <= 3 else None,
                    "as_of_date": row.date,
                    "season": season,
                    "gp": row.gp,
                    "fpts": row.fpts,
                    "pts": row.pts,
                    "reb": row.reb,
                    "ast": row.ast,
                    "stl": row.stl,
                    "blk": row.blk,
                    "tov": row.tov,
                    "min": row.min,
                    "fgm": row.fgm,
                    "fga": row.fga,
                    "fg3m": row.fg3m,
                    "fg3a": row.fg3a,
                    "ftm": row.ftm,
                    "fta": row.fta,
                    "rank": row.rank,
                    "rost_pct": row.rost_pct,
                    "pipeline_run_id": row.pipeline_run_id,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                })

            # Batch insert with conflict handling
            PlayerSeasonStats.insert_many(records).on_conflict(
                conflict_target=[PlayerSeasonStats.player, PlayerSeasonStats.as_of_date],
                preserve=[
                    PlayerSeasonStats.gp,
                    PlayerSeasonStats.fpts,
                    PlayerSeasonStats.pts,
                    PlayerSeasonStats.reb,
                    PlayerSeasonStats.ast,
                    PlayerSeasonStats.stl,
                    PlayerSeasonStats.blk,
                    PlayerSeasonStats.tov,
                    PlayerSeasonStats.min,
                    PlayerSeasonStats.fgm,
                    PlayerSeasonStats.fga,
                    PlayerSeasonStats.fg3m,
                    PlayerSeasonStats.fg3a,
                    PlayerSeasonStats.ftm,
                    PlayerSeasonStats.fta,
                    PlayerSeasonStats.rank,
                    PlayerSeasonStats.rost_pct,
                ],
            ).execute()

            count += len(records)
            if count % 5000 == 0:
                self.log(f"  Migrated {count}/{total} season stats...")

        self.stats["season_stats"] = count
        self.log(f"Migrated {count} season stat records")

    def migrate_ownership(self):
        """Extract ownership history from daily stats."""
        self.log("Migrating ownership history...")

        # Get records with ownership data
        query = (
            DailyPlayerStats.select(
                DailyPlayerStats.id,
                DailyPlayerStats.date,
                DailyPlayerStats.rost_pct,
            )
            .where(DailyPlayerStats.rost_pct.is_null(False))
            .distinct()
            .order_by(DailyPlayerStats.date, DailyPlayerStats.id)
        )

        total = query.count()
        self.log(f"Found {total} ownership records to migrate")

        if self.dry_run:
            self.stats["ownership"] = total
            self.log(f"Would migrate {total} ownership records")
            return

        count = 0
        for batch in batched(query.iterator(), self.batch_size):
            records = []
            for row in batch:
                records.append({
                    "player_id": row.id,
                    "snapshot_date": row.date,
                    "rost_pct": row.rost_pct,
                })

            # Batch insert with conflict handling
            PlayerOwnership.insert_many(records).on_conflict(
                conflict_target=[PlayerOwnership.player, PlayerOwnership.snapshot_date],
                preserve=[PlayerOwnership.rost_pct],
            ).execute()

            count += len(records)
            if count % 5000 == 0:
                self.log(f"  Migrated {count}/{total} ownership records...")

        self.stats["ownership"] = count
        self.log(f"Migrated {count} ownership records")

    def run(self, tables: list[str] | None = None):
        """Run the full migration."""
        self.log("=" * 60)
        self.log("Starting migration from stats_s2 to nba schema")
        self.log("=" * 60)

        all_tables = ["teams", "players", "game_stats", "season_stats", "ownership"]
        tables_to_migrate = tables or all_tables

        start_time = datetime.now()

        with db.atomic():
            if "teams" in tables_to_migrate:
                self.migrate_teams()

            if "players" in tables_to_migrate:
                self.migrate_players()

            if "game_stats" in tables_to_migrate:
                self.migrate_game_stats()

            if "season_stats" in tables_to_migrate:
                self.migrate_season_stats()

            if "ownership" in tables_to_migrate:
                self.migrate_ownership()

            if self.dry_run:
                self.log("Rolling back (dry run)")
                db.rollback()

        elapsed = datetime.now() - start_time
        self.log("=" * 60)
        self.log("Migration Summary")
        self.log("=" * 60)
        self.log(f"  Teams:        {self.stats['teams']:,}")
        self.log(f"  Players:      {self.stats['players']:,}")
        self.log(f"  Game Stats:   {self.stats['game_stats']:,}")
        self.log(f"  Season Stats: {self.stats['season_stats']:,}")
        self.log(f"  Ownership:    {self.stats['ownership']:,}")
        self.log(f"  Time:         {elapsed}")
        self.log("=" * 60)


def main():
    parser = argparse.ArgumentParser(
        description="Migrate data from stats_s2 to nba schema"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate migration without making changes",
    )
    parser.add_argument(
        "--tables",
        type=str,
        help="Comma-separated list of tables to migrate (teams,players,game_stats,season_stats,ownership)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=1000,
        help="Batch size for inserts (default: 1000)",
    )

    args = parser.parse_args()

    # Parse tables argument
    tables = None
    if args.tables:
        tables = [t.strip() for t in args.tables.split(",")]

    # Initialize database
    init_db()

    # Run migration
    runner = MigrationRunner(dry_run=args.dry_run, batch_size=args.batch_size)
    runner.run(tables=tables)


if __name__ == "__main__":
    main()
