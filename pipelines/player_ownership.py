"""
Player Ownership Pipeline

Fetches ESPN fantasy ownership percentages for all players and records
daily snapshots, regardless of whether players had games that day.
"""

from datetime import timedelta

from db.models.nba import Player, PlayerOwnership
from pipelines.base import BasePipeline
from pipelines.config import PipelineConfig
from pipelines.context import PipelineContext
from pipelines.extractors import ESPNExtractor


class PlayerOwnershipPipeline(BasePipeline):
    """
    Fetch ESPN ownership data for all players and record daily snapshots.

    This pipeline runs independently from game stats so that ownership
    data is captured for every player every day, not just those who played.
    """

    config = PipelineConfig(
        name="player_ownership",
        display_name="Player Ownership",
        description="Fetches ESPN fantasy ownership percentages for all players",
        target_table="nba.player_ownership",
    )

    def __init__(self):
        super().__init__()
        self.espn_extractor = ESPNExtractor()

    def execute(self, ctx: PipelineContext) -> None:
        """Execute the player ownership pipeline."""
        yesterday = ctx.started_at - timedelta(days=1)
        snapshot_date = yesterday.date()

        ctx.log.info("fetching_espn_ownership", snapshot_date=str(snapshot_date))

        espn_data = self.espn_extractor.get_player_data()
        ctx.log.info("espn_data_fetched", player_count=len(espn_data))

        for normalized_name, info in espn_data.items():
            rost_pct = info.get("rost_pct")
            if rost_pct is None:
                continue

            player = Player.find_by_name(normalized_name)
            if player is None:
                continue

            PlayerOwnership.record_ownership(
                player_id=player.id,
                snapshot_date=snapshot_date,
                rost_pct=rost_pct,
                pipeline_run_id=ctx.run_id,
            )
            ctx.increment_records()

        ctx.log.info("ownership_snapshot_complete", records=ctx.records_processed)
