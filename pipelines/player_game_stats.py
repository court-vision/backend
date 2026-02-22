"""
Daily Player Stats Pipeline

Fetches yesterday's game stats from NBA API and ESPN ownership data,
then inserts into the nba schema tables.
"""

from datetime import timedelta

import pandas as pd
import pytz

from core.settings import settings
from db.models.nba import Player, PlayerGameStats
from pipelines.base import BasePipeline
from pipelines.config import PipelineConfig
from pipelines.context import PipelineContext
from pipelines.extractors import ESPNExtractor, NBAApiExtractor
from pipelines.transformers import normalize_name, calculate_fantasy_points, minutes_to_int


class PlayerGameStatsPipeline(BasePipeline):
    """
    Fetch yesterday's game stats from NBA API and insert into player_game_stats.

    This pipeline:
    1. Fetches ESPN player data for ESPN IDs
    2. Fetches NBA game logs for yesterday
    3. Calculates fantasy points for each player
    4. Upserts player dimension records
    5. Inserts game stats into nba.player_game_stats
    """

    config = PipelineConfig(
        name="player_game_stats",
        display_name="Player Game Stats",
        description="Fetches yesterday's game stats from NBA API and ESPN ownership data",
        target_table="nba.player_game_stats",
    )

    def __init__(self):
        super().__init__()
        self.espn_extractor = ESPNExtractor()
        self.nba_extractor = NBAApiExtractor()

    def execute(self, ctx: PipelineContext) -> None:
        """Execute the daily player stats pipeline."""
        central_tz = pytz.timezone("US/Central")

        # Determine the NBA game date. Use an explicit override for backfills;
        # otherwise use CST with a 6am cutoff (before 6am = previous night's games).
        if ctx.date_override:
            game_date = ctx.date_override
        else:
            now_cst = ctx.started_at  # already in CST from PipelineContext
            if now_cst.hour < 6:
                game_date = (now_cst - timedelta(days=1)).date()
            else:
                game_date = now_cst.date()
        date_str = game_date.strftime("%m/%d/%Y")

        # Determine season string (season starts in October)
        season = f"{game_date.year}-{str(game_date.year + 1)[-2:]}"
        if game_date.month < 8:
            season = f"{game_date.year - 1}-{str(game_date.year)[-2:]}"

        ctx.log.info("fetching_data", date=date_str, season=season)

        # Fetch ESPN player data for roster percentages
        espn_data = self.espn_extractor.get_player_data()
        ctx.log.info("espn_data_fetched", player_count=len(espn_data))

        # Fetch NBA game logs
        stats = self.nba_extractor.get_game_logs(date_str, season)

        if stats.empty:
            ctx.log.info("no_games_found", date=date_str)
            return

        ctx.log.info("nba_data_fetched", record_count=len(stats))

        # Process each player
        for _, row in stats.iterrows():
            minutes_value = row["MIN"]
            if pd.isna(minutes_value) or minutes_value == "" or minutes_value is None:
                continue

            minutes_int = minutes_to_int(minutes_value)
            if minutes_int == 0:
                continue

            player_id = int(row["PLAYER_ID"])
            player_name = row["PLAYER_NAME"]
            normalized_name = normalize_name(player_name)
            team_abbrev = row["TEAM_ABBREVIATION"]

            # Get ESPN data if available
            espn_info = espn_data.get(normalized_name)
            espn_id = espn_info["espn_id"] if espn_info else None

            # Calculate stats
            player_stats = {
                "pts": int(row["PTS"]),
                "reb": int(row["REB"]),
                "ast": int(row["AST"]),
                "stl": int(row["STL"]),
                "blk": int(row["BLK"]),
                "tov": int(row["TOV"]),
                "fgm": int(row["FGM"]),
                "fga": int(row["FGA"]),
                "fg3m": int(row["FG3M"]),
                "fg3a": int(row["FG3A"]),
                "ftm": int(row["FTM"]),
                "fta": int(row["FTA"]),
            }
            fpts = calculate_fantasy_points(player_stats)

            # Upsert player dimension record
            Player.upsert_player(
                player_id=player_id,
                name=player_name,
                espn_id=espn_id,
            )

            # Insert game stats
            PlayerGameStats.upsert_game_stats(
                player_id=player_id,
                game_date=game_date,
                stats={
                    "fpts": fpts,
                    "min": minutes_int,
                    **player_stats,
                },
                team_id=team_abbrev,
                pipeline_run_id=ctx.run_id,
            )

            ctx.increment_records()
