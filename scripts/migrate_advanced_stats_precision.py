#!/usr/bin/env python
"""
Migration Script: Increase decimal precision for advanced stats percentage columns.

The NBA API returns percentage stats as decimals (e.g., USG_PCT=0.234),
but the original schema used decimal_places=1, which rounded 0.234 to 0.2.

This migration increases decimal_places from 1 to 3 for:
- usg_pct, ast_pct, ast_ratio, reb_pct, oreb_pct, dreb_pct, tov_pct

Usage:
    # Dry run (show SQL without executing)
    python scripts/migrate_advanced_stats_precision.py --dry-run

    # Execute migration
    python scripts/migrate_advanced_stats_precision.py
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.base import db, init_db


COLUMNS_TO_ALTER = [
    "usg_pct",
    "ast_pct",
    "ast_ratio",
    "reb_pct",
    "oreb_pct",
    "dreb_pct",
    "tov_pct",
]

TABLE = "nba.player_advanced_stats"


def migrate(dry_run: bool = False):
    init_db()

    statements = []
    for col in COLUMNS_TO_ALTER:
        # NUMERIC(6,3) = max_digits=6, decimal_places=3
        sql = f"ALTER TABLE {TABLE} ALTER COLUMN {col} TYPE NUMERIC(6,3);"
        statements.append(sql)

    print(f"Migration: Increase precision for {len(statements)} columns in {TABLE}")
    print()

    for sql in statements:
        print(f"  {sql}")

    if dry_run:
        print("\n[DRY RUN] No changes made.")
        return

    print()
    with db.atomic():
        for sql in statements:
            db.execute_sql(sql)
            print(f"  Executed: {sql}")

    print(f"\nMigration complete. {len(statements)} columns updated.")
    print("Note: Existing data retains its rounded values. Run the advanced_stats")
    print("pipeline to refresh with full precision.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Migrate advanced stats precision")
    parser.add_argument("--dry-run", action="store_true", help="Show SQL without executing")
    args = parser.parse_args()
    migrate(dry_run=args.dry_run)
