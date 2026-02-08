"""
Injury Report Pipeline

Fetches player injury data from nbainjuries package.
"""

from datetime import date

import pytz

from db.models.nba import Player, PlayerInjury
from pipelines.base import BasePipeline
from pipelines.config import PipelineConfig
from pipelines.context import PipelineContext
from pipelines.extractors import InjuriesExtractor
from pipelines.transformers import normalize_name


class InjuryReportPipeline(BasePipeline):
    """
    Fetch current injury report and insert into player_injuries.

    This pipeline:
    1. Fetches current injuries from nbainjuries package
    2. Matches players to our player dimension table
    3. Inserts/updates injury records

    Note: Requires nbainjuries package to be installed.
    """

    config = PipelineConfig(
        name="injury_report",
        display_name="Injury Report",
        description="Fetches player injury status from nbainjuries package",
        target_table="nba.player_injuries",
    )

    def __init__(self):
        super().__init__()
        self.injuries_extractor = InjuriesExtractor()

    def execute(self, ctx: PipelineContext) -> None:
        """Execute the injury report pipeline."""
        central_tz = pytz.timezone("US/Central")
        today = ctx.started_at.date()

        ctx.log.info("fetching_injury_report", date=str(today))

        try:
            # Fetch current injuries
            raw_injuries = self.injuries_extractor.get_current_injuries()
        except ImportError as e:
            ctx.log.error("package_not_installed", error=str(e))
            raise

        if not raw_injuries:
            ctx.log.info("no_injuries_found")
            return

        ctx.log.info("injuries_fetched", count=len(raw_injuries))

        # Build player lookup by normalized name
        player_lookup = self._build_player_lookup()
        ctx.log.info("player_lookup_built", player_count=len(player_lookup))

        # Process each injury
        matched = 0
        unmatched = 0

        for raw_injury in raw_injuries:
            # Normalize injury data
            injury_data = self.injuries_extractor.normalize_injury_data(raw_injury)

            # Try to match player
            player_name = injury_data.get("player_name", "")
            normalized = normalize_name(player_name)
            player_id = player_lookup.get(normalized)

            if not player_id:
                # Try partial match (last name)
                player_id = self._fuzzy_match_player(player_name, player_lookup)

            if player_id:
                # Insert/update injury record
                PlayerInjury.upsert_injury(
                    player_id=player_id,
                    report_date=today,
                    status=injury_data.get("status", "Unknown"),
                    injury_type=injury_data.get("injury_type"),
                    injury_detail=injury_data.get("injury_detail"),
                    expected_return=None,  # nbainjuries may not have this
                    pipeline_run_id=ctx.run_id,
                )
                ctx.increment_records()
                matched += 1
            else:
                ctx.log.debug(
                    "player_not_matched",
                    player_name=player_name,
                    status=injury_data.get("status"),
                )
                unmatched += 1

        ctx.log.info(
            "processing_complete",
            matched=matched,
            unmatched=unmatched,
            records=ctx.records_processed,
        )

    def _build_player_lookup(self) -> dict[str, int]:
        """Build lookup dict mapping normalized name to player_id."""
        from db.models.nba import Player

        lookup = {}
        for player in Player.select(Player.id, Player.name_normalized):
            if player.name_normalized:
                lookup[player.name_normalized] = player.id
        return lookup

    def _fuzzy_match_player(
        self,
        player_name: str,
        lookup: dict[str, int],
    ) -> int | None:
        """
        Try to fuzzy match a player name.

        Attempts:
        1. Last name only match
        2. First name + last initial match
        """
        if not player_name:
            return None

        parts = player_name.split()
        if not parts:
            return None

        # Try last name only (may have collisions, but better than nothing)
        last_name = normalize_name(parts[-1])
        matches = [
            pid for name, pid in lookup.items()
            if name.endswith(last_name)
        ]

        if len(matches) == 1:
            return matches[0]

        # If multiple matches and we have first name, try to narrow down
        if len(matches) > 1 and len(parts) > 1:
            first_name = normalize_name(parts[0])
            for name, pid in lookup.items():
                if name.startswith(first_name) and name.endswith(last_name):
                    return pid

        return None
