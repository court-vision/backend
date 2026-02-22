"""
Advanced Stats Pipeline

Fetches advanced player statistics (efficiency, usage, impact) from NBA API.
"""

from datetime import timedelta

import pytz

from core.settings import settings
from db.models.nba import Player, PlayerAdvancedStats
from pipelines.base import BasePipeline
from pipelines.config import PipelineConfig
from pipelines.context import PipelineContext
from pipelines.extractors import NBAApiExtractor


class PlayerAdvancedStatsPipeline(BasePipeline):
    """
    Fetch advanced player stats and insert into player_advanced_stats.

    This pipeline:
    1. Fetches LeagueDashPlayerStats with MeasureType="Advanced"
    2. Ensures player exists in dimension table
    3. Inserts/updates advanced stats records

    Advanced stats include:
    - Efficiency: OFF_RATING, DEF_RATING, NET_RATING, TS_PCT, EFG_PCT
    - Usage: USG_PCT, AST_PCT, REB_PCT, TOV_PCT
    - Impact: PACE, PIE, PLUS_MINUS
    """

    config = PipelineConfig(
        name="advanced_stats",
        display_name="Advanced Stats",
        description="Fetches advanced player stats (efficiency, usage, impact)",
        target_table="nba.player_advanced_stats",
    )

    def __init__(self):
        super().__init__()
        self.nba_extractor = NBAApiExtractor()

    def execute(self, ctx: PipelineContext) -> None:
        """Execute the advanced stats pipeline."""
        central_tz = pytz.timezone("US/Central")

        # Determine the as_of_date. Use an explicit override for backfills;
        # otherwise use CST with a 6am cutoff (before 6am = previous night's games).
        if ctx.date_override:
            as_of_date = ctx.date_override
        else:
            now_cst = ctx.started_at  # already in CST from PipelineContext
            if now_cst.hour < 6:
                as_of_date = (now_cst - timedelta(days=1)).date()
            else:
                as_of_date = now_cst.date()

        # Determine season string
        season = f"{as_of_date.year}-{str(as_of_date.year + 1)[-2:]}"
        if as_of_date.month < 8:
            season = f"{as_of_date.year - 1}-{str(as_of_date.year)[-2:]}"

        ctx.log.info("fetching_advanced_stats", season=season, as_of_date=str(as_of_date))

        # Fetch advanced stats from NBA API
        api_data = self.nba_extractor.get_advanced_stats(season)

        if not api_data:
            ctx.log.info("no_data_returned")
            return

        ctx.log.info("data_fetched", player_count=len(api_data))

        # Process each player
        for player in api_data:
            player_id = player["PLAYER_ID"]
            player_name = player["PLAYER_NAME"]
            team_abbrev = player.get("TEAM_ABBREVIATION")

            # Ensure player exists in dimension table
            Player.upsert_player(player_id=player_id, name=player_name)

            # Prepare stats dict
            stats = {
                "gp": player.get("GP"),
                "min": player.get("MIN"),
                "off_rating": player.get("OFF_RATING"),
                "def_rating": player.get("DEF_RATING"),
                "net_rating": player.get("NET_RATING"),
                "ts_pct": player.get("TS_PCT"),
                "efg_pct": player.get("EFG_PCT"),
                "usg_pct": player.get("USG_PCT"),
                "ast_pct": player.get("AST_PCT"),
                "ast_to_tov": player.get("AST_TO"),
                "ast_ratio": player.get("AST_RATIO"),
                "reb_pct": player.get("REB_PCT"),
                "oreb_pct": player.get("OREB_PCT"),
                "dreb_pct": player.get("DREB_PCT"),
                "tov_pct": player.get("TM_TOV_PCT"),
                "pace": player.get("PACE"),
                "pie": player.get("PIE"),
                "poss": player.get("POSS"),
                "plus_minus": player.get("PLUS_MINUS"),
            }

            # Upsert advanced stats
            PlayerAdvancedStats.upsert_advanced_stats(
                player_id=player_id,
                as_of_date=as_of_date,
                season=season,
                stats=stats,
                team_id=team_abbrev if team_abbrev and len(team_abbrev) <= 3 else None,
                pipeline_run_id=ctx.run_id,
            )

            ctx.increment_records()

        ctx.log.info("processing_complete", records=ctx.records_processed)
