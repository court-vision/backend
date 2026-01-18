#!/usr/bin/env python3
"""
Pipeline Runner Script

Standalone script for running data pipelines via Railway cron or command line.

Usage:
    python scripts/run_pipelines.py              # Run all pipelines
    python scripts/run_pipelines.py --daily      # Run daily player stats only
    python scripts/run_pipelines.py --cumulative # Run cumulative stats only
    python scripts/run_pipelines.py --matchup    # Run matchup scores only

Railway Cron Command:
    python scripts/run_pipelines.py
"""

import sys
import os

# Add the backend directory to the Python path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import asyncio
import argparse
from datetime import datetime

import pytz

from db.base import init_db, close_db
from services.pipeline_service import PipelineService
from schemas.common import ApiStatus
from schemas.pipeline import PipelineResult


def print_result(name: str, result: PipelineResult) -> None:
    """Print pipeline result in a readable format."""
    status_icon = "✓" if result.status == ApiStatus.SUCCESS else "✗"
    print(f"\n{status_icon} {name}")
    print(f"  Status: {result.status}")
    print(f"  Message: {result.message}")
    if result.records_processed is not None:
        print(f"  Records: {result.records_processed}")
    if result.duration_seconds is not None:
        print(f"  Duration: {result.duration_seconds:.2f}s")
    if result.error:
        print(f"  Error: {result.error[:200]}...")


async def main():
    parser = argparse.ArgumentParser(
        description="Run data pipelines for the fantasy basketball platform"
    )
    parser.add_argument(
        "--daily",
        action="store_true",
        help="Run only the daily player stats pipeline",
    )
    parser.add_argument(
        "--cumulative",
        action="store_true",
        help="Run only the cumulative player stats pipeline",
    )
    parser.add_argument(
        "--matchup",
        action="store_true",
        help="Run only the daily matchup scores pipeline",
    )
    args = parser.parse_args()

    # Initialize database connection
    print("Initializing database connection...")
    init_db()

    central_tz = pytz.timezone("US/Central")
    start_time = datetime.now(central_tz)
    print(f"Pipeline run started at {start_time.isoformat()}")

    try:
        if args.daily:
            print("\n" + "=" * 50)
            print("Running: Daily Player Stats")
            print("=" * 50)
            result = await PipelineService.run_daily_player_stats()
            print_result("Daily Player Stats", result)

        elif args.cumulative:
            print("\n" + "=" * 50)
            print("Running: Cumulative Player Stats")
            print("=" * 50)
            result = await PipelineService.run_cumulative_player_stats()
            print_result("Cumulative Player Stats", result)

        elif args.matchup:
            print("\n" + "=" * 50)
            print("Running: Daily Matchup Scores")
            print("=" * 50)
            result = await PipelineService.run_daily_matchup_scores()
            print_result("Daily Matchup Scores", result)

        else:
            # Run all pipelines
            results = await PipelineService.run_all_pipelines()

            print("\n" + "=" * 50)
            print("RESULTS SUMMARY")
            print("=" * 50)

            for name, result in results.items():
                print_result(name.replace("_", " ").title(), result)

            # Check for any failures
            failures = [
                name
                for name, result in results.items()
                if result.status != ApiStatus.SUCCESS
            ]
            if failures:
                print(f"\n⚠ {len(failures)} pipeline(s) failed: {', '.join(failures)}")
                sys.exit(1)

    finally:
        # Close database connection
        close_db()

    end_time = datetime.now(central_tz)
    duration = (end_time - start_time).total_seconds()
    print(f"\nTotal duration: {duration:.2f}s")
    print("Pipeline run completed successfully!")


if __name__ == "__main__":
    asyncio.run(main())
